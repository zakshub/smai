"""Shared end-to-end orchestration pipeline for SMAI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from smai.agents import AnalystAgent, CriticAgent, LLMClient, ScoutAgent, WriterAgent
from smai.config import BrandBrain, Settings, load_brand
from smai.db.database import Database
from smai.db.models import (
    ApprovalRequiredError,
    ContentFormat,
    ContentPackage,
    CycleMode,
    DuplicatePublishError,
    InvalidStateTransitionError,
    QAReport,
    TopicCandidate,
    normalize_approval_status,
)
from smai.media import CarouselBuilder, ReelBuilder
from smai.publishing import (
    InstagramBrowserAutomator,
    InstagramGraphClient,
    MediaStagingService,
    MetaAPIError,
    export_draft_bundle,
    redact_secret,
)


class SmaiPipeline:
    """Coordinates Scout -> Strategist -> Writer -> Media Render -> Critic QA -> Draft/Publish."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        graph_client: InstagramGraphClient | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.llm = LLMClient(settings, db=db)
        self.scout = ScoutAgent(self.llm, db)
        self.writer = WriterAgent(self.llm)
        self.critic = CriticAgent()
        self.analyst = AnalystAgent(db)
        self.carousel_builder = CarouselBuilder()
        self.reel_builder = ReelBuilder(settings)
        self._graph_client = graph_client

    def ensure_brand(self, slug: str | None = None) -> tuple[BrandBrain, int]:
        target = slug or self.settings.smai_default_brand
        brand = load_brand(target, self.settings)
        brand_id = self.db.upsert_brand(brand, ig_user_id=self.settings.ig_user_id)
        return brand, brand_id

    async def run_scout(self, brand_slug: str | None = None, limit: int = 5) -> list[TopicCandidate]:
        brand, brand_id = self.ensure_brand(brand_slug)
        summary = self.analyst.build_feedback_summary()
        return await self.scout.discover(
            brand=brand,
            brand_id=brand_id,
            limit=limit,
            insights_summary=summary,
            enable_network=self.settings.smai_enable_network_scout,
        )

    async def run_full_draft_cycle(
        self,
        brand_slug: str | None = None,
        formats: list[ContentFormat] | None = None,
        render_media: bool = True,
        dry_run_reel: bool = False,
        cycle_mode: CycleMode = "draft_only",
        approve_qa_passed: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Execute the research-to-draft cycle without mixing scheduling and export/publishing:
        - draft_only: Generate, render, and QA into 'pending_approval'.
        - export_review: Requires approval; exports approved/QA-passed bundles without scheduling.
        - auto_schedule: Requires approval; schedules into a posting window without exporting/publishing immediately.
        """
        brand, brand_id = self.ensure_brand(brand_slug)
        target_formats: list[ContentFormat] = formats or [
            f for f in brand.regular_cadence.daily_mix
            if f in ("carousel", "reel", "slide_video", "single_pic", "text_post", "story")  # type: ignore[comparison-overlap]
        ] or ["reel", "slide_video", "carousel", "single_pic", "text_post"]

        await self.run_scout(brand_slug=brand.slug, limit=max(len(target_formats), 6))
        results: list[dict[str, Any]] = []
        for fmt in target_formats:
            unused = self.db.list_topics(brand_id, status="new", limit=1)
            topic_id = int(unused[0]["id"]) if unused else None
            res = await self.generate_draft(
                fmt=fmt,
                brand_slug=brand.slug,
                topic_id=topic_id,
                render_media=render_media,
                dry_run_reel=dry_run_reel,
            )
            draft_id = int(res["draft"]["id"])
            if cycle_mode in ("export_review", "auto_schedule") and res["qa"]["status"] == "pass":
                if approve_qa_passed:
                    self.approve_draft(draft_id)
                    if cycle_mode == "export_review":
                        exp = await self.export_draft(draft_id)
                        res["export"] = exp
                    elif cycle_mode == "auto_schedule":
                        sched = self.schedule_draft(draft_id)
                        res["schedule"] = sched
                    res["draft"] = self.db.get_draft(draft_id)
                else:
                    res["cycle_note"] = (
                        f"Draft stays in 'pending_approval': '{cycle_mode}' never runs without approval."
                    )
            results.append(res)
        return results

    async def generate_draft(
        self,
        fmt: ContentFormat | None = None,
        brand_slug: str | None = None,
        topic_id: int | None = None,
        custom_topic: str | None = None,
        render_media: bool = True,
        prefer_playwright: bool | None = None,
        dry_run_reel: bool = False,
    ) -> dict[str, Any]:
        brand, brand_id = self.ensure_brand(brand_slug)
        insights_summary = self.analyst.build_feedback_summary()

        chosen_topic_id: int | None = topic_id
        if custom_topic:
            rec = self.scout.strategist.recommend(brand, custom_topic, preferred_format=fmt)
            topic = TopicCandidate(
                title=custom_topic,
                angle="Custom operator prompt",
                pillar=brand.pillars[0] if brand.pillars else brand.niche,
                viral_score=8.5,
                suggested_format=fmt or rec.suggested_format,
                target_audience=rec.target_audience,
                recommended_time=rec.recommended_time,
                algorithm_rationale=rec.algorithm_rationale,
            )
            chosen_topic_id = self.db.insert_topic(
                brand_id=brand_id,
                title=topic.title,
                angle=topic.angle,
                pillar=topic.pillar,
                viral_score=topic.viral_score,
                suggested_format=topic.suggested_format,
                target_audience=topic.target_audience,
                recommended_time=topic.recommended_time,
                algorithm_rationale=topic.algorithm_rationale,
            )
        elif chosen_topic_id is not None:
            topics = self.db.list_topics(brand_id, limit=100)
            match = next((t for t in topics if t["id"] == chosen_topic_id), None)
            if not match:
                raise ValueError(f"Topic ID {chosen_topic_id} not found.")
            topic = TopicCandidate(
                title=match["title"],
                angle=match["angle"],
                pillar=match["pillar"],
                viral_score=match["viral_score"],
                source_url=match["source_url"],
                rationale=match["rationale"],
                suggested_format=match.get("suggested_format") or "carousel",
                target_audience=match.get("target_audience") or "",
                recommended_time=match.get("recommended_time") or "",
                algorithm_rationale=match.get("algorithm_rationale") or "",
            )
        else:
            new_topics = self.db.list_topics(brand_id, status="new", limit=1)
            if not new_topics:
                await self.run_scout(brand_slug=brand.slug, limit=3)
                new_topics = self.db.list_topics(brand_id, status="new", limit=1)
            if new_topics:
                row = new_topics[0]
                chosen_topic_id = int(row["id"])
                topic = TopicCandidate(
                    title=row["title"],
                    angle=row["angle"],
                    pillar=row["pillar"],
                    viral_score=row["viral_score"],
                    source_url=row["source_url"],
                    rationale=row["rationale"],
                    suggested_format=row.get("suggested_format") or "carousel",
                    target_audience=row.get("target_audience") or "",
                    recommended_time=row.get("recommended_time") or "",
                    algorithm_rationale=row.get("algorithm_rationale") or "",
                )
            else:
                topic = TopicCandidate(
                    title="3-step AI morning workflow to save 5 hours every week",
                    angle="Actionable breakdown",
                    pillar=brand.pillars[0] if brand.pillars else brand.niche,
                )

        target_fmt: ContentFormat = fmt or topic.suggested_format

        # Write + Critic auto-revision loop (max 1 revision pass)
        package = await self.writer.write(
            brand=brand, topic=topic, fmt=target_fmt, insights_summary=insights_summary
        )
        qa: QAReport = self.critic.evaluate(brand, package)
        if qa.status == "revise" and qa.revision_instructions:
            package = await self.writer.write(
                brand=brand,
                topic=topic,
                fmt=target_fmt,
                insights_summary=insights_summary,
                revision_notes=qa.revision_instructions,
            )

        draft_id = self.db.insert_draft(
            brand_id=brand_id,
            topic_id=chosen_topic_id,
            fmt=target_fmt,
            title=package.title,
            script_json=package.model_dump_json(),
            caption=package.caption,
            hashtags=" ".join(package.hashtags),
            ab_hooks_json=json.dumps([h.model_dump() for h in package.ab_hooks]),
            target_audience=package.target_audience,
            recommended_time=package.recommended_time,
            algorithm_rationale=package.algorithm_rationale,
        )
        if chosen_topic_id is not None:
            self.db.mark_topic_status(chosen_topic_id, "used")

        assets: list[dict[str, Any]] = []
        if render_media:
            use_pw = (
                self.settings.smai_prefer_playwright
                if prefer_playwright is None
                else prefer_playwright
            )
            assets = await self.render_draft(
                draft_id=draft_id,
                brand=brand,
                package=package,
                prefer_playwright=use_pw,
                dry_run_reel=dry_run_reel,
            )

        final_qa = self.critic.evaluate(brand, package, rendered_assets=assets if render_media else None)
        self.db.update_draft_qa(
            draft_id=draft_id,
            qa_status=final_qa.status,
            qa_score=final_qa.score,
            qa_notes=[c.detail for c in final_qa.checks if not c.passed],
        )

        return {
            "draft": self.db.get_draft(draft_id),
            "assets": self.db.list_media_assets(draft_id),
            "qa": final_qa.model_dump(),
        }

    async def render_draft(
        self,
        draft_id: int,
        brand: BrandBrain | None = None,
        package: ContentPackage | None = None,
        prefer_playwright: bool = True,
        dry_run_reel: bool = False,
    ) -> list[dict[str, Any]]:
        draft = self.db.get_draft(draft_id)
        if not draft:
            raise ValueError(f"Draft {draft_id} not found.")
        if package is None:
            package = ContentPackage.model_validate_json(draft["script_json"])
        if brand is None:
            brand, _ = self.ensure_brand()

        out_dir = self.settings.media_dir() / f"draft_{draft_id}"
        if package.format in ("carousel", "story", "single_pic", "text_post"):
            assets = await self.carousel_builder.build(
                brand=brand,
                package=package,
                output_dir=out_dir,
                prefer_playwright=prefer_playwright,
            )
        else:
            reel_asset = await self.reel_builder.build(
                brand=brand,
                package=package,
                output_dir=out_dir,
                dry_run=dry_run_reel,
            )
            assets = [reel_asset]

        self.db.replace_media_assets(draft_id, assets)
        return assets

    def approve_draft(self, draft_id: int) -> dict[str, Any]:
        return self.db.transition_draft_status(draft_id, "approved")

    def reject_draft(self, draft_id: int) -> dict[str, Any]:
        return self.db.transition_draft_status(draft_id, "rejected")

    def reopen_draft(self, draft_id: int, target_status: str = "pending_approval") -> dict[str, Any]:
        return self.db.transition_draft_status(draft_id, target_status)

    def schedule_draft(self, draft_id: int, scheduled_for: str | None = None) -> dict[str, Any]:
        from smai.scheduler.worker import pick_next_window_slot

        draft = self.db.get_draft(draft_id)
        if not draft:
            raise ValueError(f"Draft {draft_id} not found.")
        brand, _ = self.ensure_brand()
        slot_iso = (
            scheduled_for
            or pick_next_window_slot(brand.schedule_windows, timezone=brand.timezone).isoformat()
        )
        schedule_id = self.db.schedule_draft_atomic(draft_id, slot_iso)
        return {
            "schedule_id": schedule_id,
            "draft_id": draft_id,
            "scheduled_for": slot_iso,
            "draft": self.db.get_draft(draft_id),
        }

    def cancel_schedule(self, schedule_id: int) -> dict[str, Any]:
        return self.db.cancel_schedule_atomic(schedule_id)

    async def export_draft(self, draft_id: int) -> dict[str, Any]:
        """Export an approved (or exported/scheduled) draft into a local bundle without marking published."""
        draft = self.db.get_draft(draft_id)
        if not draft:
            raise ValueError(f"Draft {draft_id} not found.")
        cur_status = normalize_approval_status(str(draft["approval_status"]))
        if cur_status == "published":
            raise DuplicatePublishError(f"Draft {draft_id} is already published.")
        if cur_status in ("draft", "pending_approval", "rejected", "failed"):
            raise ApprovalRequiredError(
                f"Draft {draft_id} has status '{cur_status}' and must be approved before exporting."
            )
        if cur_status not in ("approved", "exported", "scheduled"):
            raise InvalidStateTransitionError(
                f"Cannot export draft {draft_id} from status '{cur_status}'."
            )

        assets = self.db.list_media_assets(draft_id)
        if not assets:
            await self.render_draft(draft_id)
            assets = self.db.list_media_assets(draft_id)

        bundle_dir = export_draft_bundle(draft, assets, self.settings.resolve_export_dir())
        log_id = self.db.record_export(draft_id=draft_id, export_path=str(bundle_dir))
        return {
            "publish_log_id": log_id,
            "mode": "export",
            "outcome": "exported",
            "export_path": str(bundle_dir),
            "draft": self.db.get_draft(draft_id),
        }

    async def stage_in_browser(self, draft_id: int) -> dict[str, Any]:
        """Upload media & caption into IG Web Composer without clicking Share (records 'staged', not 'published')."""
        draft = self.db.get_draft(draft_id)
        if not draft:
            raise ValueError(f"Draft {draft_id} not found.")
        cur_status = normalize_approval_status(str(draft["approval_status"]))
        if cur_status == "published":
            raise DuplicatePublishError(f"Draft {draft_id} is already published.")
        if cur_status in ("draft", "pending_approval", "rejected", "failed"):
            raise ApprovalRequiredError(
                f"Draft {draft_id} has status '{cur_status}' and must be approved before staging."
            )
        if cur_status not in ("approved", "exported", "scheduled"):
            raise InvalidStateTransitionError(
                f"Cannot stage draft {draft_id} from status '{cur_status}'."
            )

        assets = self.db.list_media_assets(draft_id)
        if not assets:
            await self.render_draft(draft_id)
            assets = self.db.list_media_assets(draft_id)
        image_assets = [a for a in assets if a["asset_type"] == "slide_png"]
        if draft["format"] in ("reel", "slide_video"):
            upload_paths = [Path(a["local_path"]) for a in assets if a["local_path"].endswith(".mp4")]
        else:
            upload_paths = [Path(a["local_path"]) for a in image_assets]

        automator = InstagramBrowserAutomator(self.settings)
        res_browser = await automator.upload_draft_to_instagram(
            media_paths=upload_paths,
            caption=draft["caption"],
            auto_share=False,
        )
        log_id = self.db.record_staged(
            draft_id=draft_id,
            mode="ig_browser_stage",
            notes=",".join(res_browser.get("files_uploaded", [])),
        )
        return {
            "publish_log_id": log_id,
            **res_browser,
            "draft": self.db.get_draft(draft_id),
        }

    def confirm_staged_publish(
        self, draft_id: int, ig_media_id: str = "", permalink: str = ""
    ) -> dict[str, Any]:
        log_id = self.db.confirm_staged_publish(
            draft_id=draft_id, ig_media_id=ig_media_id, permalink=permalink
        )
        return {
            "publish_log_id": log_id,
            "mode": "ig_browser_confirmed",
            "outcome": "published",
            "draft": self.db.get_draft(draft_id),
        }

    async def publish_draft(
        self,
        draft_id: int,
        mode: str | None = None,
        verify_public_media: bool = True,
    ) -> dict[str, Any]:
        """Publish an approved draft via live Meta Graph API or confirmed browser auto-share."""
        draft = self.db.get_draft(draft_id)
        if not draft:
            raise ValueError(f"Draft {draft_id} not found.")

        target_mode = (mode or self.settings.smai_publish_mode).lower().strip()
        if target_mode == "export":
            raise ValueError(
                "Export mode does not publish to Instagram. Call export_draft(draft_id) "
                "or specify mode='graph_api' / mode='browser_share' to publish."
            )
        if target_mode == "browser":
            raise ValueError(
                "Browser staging mode awaits manual Share. Call stage_in_browser(draft_id) "
                "followed by confirm_staged_publish(draft_id)."
            )
        if target_mode not in ("graph_api", "browser_share"):
            raise ValueError(f"Unsupported publish mode '{target_mode}'.")

        prev_status = self.db.claim_publish_lock(draft_id, target_mode)
        if prev_status is None:
            raise DuplicatePublishError(
                f"Draft {draft_id} has already been published or is currently publishing."
            )

        try:
            assets = self.db.list_media_assets(draft_id)
            if not assets:
                await self.render_draft(draft_id)
                assets = self.db.list_media_assets(draft_id)

            image_assets = [a for a in assets if a["asset_type"] == "slide_png"]

            if target_mode == "browser_share":
                automator = InstagramBrowserAutomator(self.settings)
                if draft["format"] in ("reel", "slide_video"):
                    upload_paths = [
                        Path(a["local_path"]) for a in assets if a["local_path"].endswith(".mp4")
                    ]
                else:
                    upload_paths = [Path(a["local_path"]) for a in image_assets]
                res_browser = await automator.upload_draft_to_instagram(
                    media_paths=upload_paths,
                    caption=draft["caption"],
                    auto_share=True,
                )
                if res_browser.get("status") == "confirmed_shared":
                    log_id = self.db.record_publish(
                        draft_id=draft_id,
                        mode="browser_share",
                        permalink="https://www.instagram.com/",
                        export_path=",".join(res_browser.get("files_uploaded", [])),
                        outcome="published",
                    )
                else:
                    log_id = self.db.record_staged(
                        draft_id=draft_id,
                        mode="browser_share_staged",
                        notes=",".join(res_browser.get("files_uploaded", [])),
                    )
                return {"publish_log_id": log_id, **res_browser}

            # target_mode == "graph_api"
            staging = MediaStagingService(self.settings)
            client = self._graph_client or InstagramGraphClient(self.settings)
            target_assets = (
                image_assets
                if draft["format"] in ("carousel", "single_pic", "text_post", "story")
                else assets
            )
            if not target_assets:
                raise ValueError(f"No publishable media assets found for draft {draft_id}.")

            urls: list[str] = []
            for asset in target_assets:
                url = staging.resolve_public_url(Path(asset["local_path"]))
                if verify_public_media:
                    expected_prefix = (
                        "video/" if draft["format"] in ("reel", "slide_video") else "image/"
                    )
                    reachability = await staging.verify_public_url_reachable(
                        url, expected_prefix=expected_prefix
                    )
                    if not reachability["ok"]:
                        raise RuntimeError(
                            f"Staged public media URL verification failed for '{url}': "
                            f"{reachability['reason']}"
                        )
                self.db.update_asset_public_url(int(asset["id"]), url)
                urls.append(url)

            if draft["format"] in ("reel", "slide_video"):
                res = await client.publish_reel(urls[0], draft["caption"])
            elif draft["format"] == "story":
                res = await client.publish_story(urls[0])
            elif len(urls) == 1:
                res = await client.publish_image(urls[0], draft["caption"])
            else:
                res = await client.publish_carousel(urls, draft["caption"])

            log_id = self.db.record_publish(
                draft_id=draft_id,
                mode="graph_api",
                ig_container_id=res.get("ig_container_id", ""),
                ig_media_id=res.get("ig_media_id", ""),
                permalink=res.get("permalink", ""),
                outcome="published",
            )
            return {"publish_log_id": log_id, "mode": "graph_api", **res}
        except Exception as exc:
            diag = redact_secret(str(exc), self.settings.ig_access_token)
            non_recoverable = isinstance(exc, MetaAPIError) and not exc.is_transient
            self.db.release_publish_lock(
                draft_id,
                prev_status,
                mark_failed=non_recoverable,
                error_message=diag,
            )
            raise
