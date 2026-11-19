"""FastAPI local studio dashboard and API for SMAI."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from smai.agents.account_analyzer import InstagramAccountAnalyzer
from smai.agents.algorithm import ALGORITHM_FORMAT_PLAYBOOK
from smai.config import Settings, list_brand_slugs, load_brand
from smai.db.database import get_database
from smai.db.models import (
    ApprovalRequiredError,
    ContentFormat,
    CycleMode,
    DuplicatePublishError,
    InsightSnapshot,
    InvalidStateTransitionError,
    normalize_approval_status,
)
from smai.pipeline import SmaiPipeline
from smai.publishing import (
    InstagramBrowserAutomator,
    InstagramGraphClient,
    MetaAPIError,
    redact_secret,
)
from smai.scheduler import SchedulerWorker


def create_app(
    settings: Settings | None = None,
    enable_scheduler: bool = True,
) -> FastAPI:
    cfg = settings or Settings()
    db = get_database(cfg)
    worker = SchedulerWorker(cfg, db)
    templates_dir = Path(__file__).resolve().parents[1] / "templates" / "web"
    templates = Jinja2Templates(directory=str(templates_dir))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        pipeline = SmaiPipeline(cfg, db)
        pipeline.ensure_brand()
        if enable_scheduler:
            worker.start()
        try:
            yield
        finally:
            if enable_scheduler:
                worker.stop()

    app = FastAPI(title="SMAI Studio", version="0.1.0", lifespan=lifespan)
    media_root = cfg.media_dir()
    app.mount("/media", StaticFiles(directory=str(media_root)), name="media")

    def _to_media_url(local_path: str) -> str:
        try:
            rel = Path(local_path).resolve().relative_to(media_root.resolve()).as_posix()
            return f"/media/{rel}"
        except Exception:
            return ""

    def _enrich_draft(draft: dict[str, Any]) -> dict[str, Any]:
        assets = db.list_media_assets(int(draft["id"]))
        for asset in assets:
            asset["url"] = _to_media_url(asset["local_path"])
        draft["assets"] = assets
        draft["ab_hooks"] = json.loads(draft.get("ab_hooks_json") or "[]")
        draft["qa_notes_list"] = json.loads(draft.get("qa_notes") or "[]")
        draft["script"] = json.loads(draft.get("script_json") or "{}")
        draft["approval_status"] = normalize_approval_status(str(draft.get("approval_status") or "draft"))
        return draft

    def _redirect_home(brand: str = "", notice: str = "", error: str = "") -> RedirectResponse:
        params: dict[str, str] = {}
        if brand:
            params["brand"] = brand
        if notice:
            params["notice"] = notice
        if error:
            params["error"] = redact_secret(error, cfg.ig_access_token)
        qs = f"?{urlencode(params)}" if params else ""
        return RedirectResponse(url=f"/{qs}", status_code=303)

    def _raise_http(exc: Exception) -> HTTPException:
        detail = redact_secret(str(exc), cfg.ig_access_token)
        if isinstance(exc, ApprovalRequiredError):
            return HTTPException(status_code=403, detail=detail)
        if isinstance(exc, (DuplicatePublishError, InvalidStateTransitionError)):
            return HTTPException(status_code=409, detail=detail)
        if isinstance(exc, MetaAPIError):
            return HTTPException(status_code=502, detail=detail)
        if isinstance(exc, ValueError):
            code = 404 if "not found" in detail.lower() else 400
            return HTTPException(status_code=code, detail=detail)
        return HTTPException(status_code=400, detail=detail)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        brand: str = "",
        notice: str = "",
        error: str = "",
    ) -> HTMLResponse:
        available_brands = list_brand_slugs(cfg)
        active_slug = brand if brand in available_brands else cfg.smai_default_brand
        pipeline = SmaiPipeline(cfg, db)
        brand_cfg, brand_id = pipeline.ensure_brand(active_slug)
        drafts = [_enrich_draft(d) for d in db.list_drafts(brand_id=brand_id, limit=30)]
        performance = pipeline.analyst.get_performance_breakdown(limit=60)
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "brand": brand_cfg,
                "available_brands": available_brands,
                "stats": db.dashboard_stats(),
                "topics": db.list_topics(brand_id, status="new", limit=10),
                "drafts": drafts,
                "schedules": db.list_schedules(limit=15),
                "publish_logs": db.list_publish_logs(limit=15),
                "insights": db.list_top_insights(limit=5),
                "performance": performance,
                "algorithm_playbook": ALGORITHM_FORMAT_PLAYBOOK,
                "account_audit": db.get_latest_account_audit(brand_cfg.slug),
                "account_snapshot": db.get_latest_account_snapshot(brand_slug=brand_cfg.slug),
                "account_media_cache": db.list_account_media_cache(limit=8),
                "experiments": db.list_experiments(limit=20),
                "experiment_learnings": db.list_experiment_learnings(limit=20),
                "llm_provider": f"{cfg.smai_llm_provider} ({cfg.smai_llm_model})",
                "gemini_key_set": bool(cfg.effective_gemini_key()),
                "ig_browser_connected": InstagramBrowserAutomator(cfg).has_saved_session(),
                "meta_api_configured": cfg.has_meta_credentials(),
                "meta_graph_version": cfg.ig_graph_api_version,
                "meta_ig_user_id": cfg.ig_user_id,
                "publish_mode": cfg.smai_publish_mode,
                "public_media_url": cfg.smai_public_media_base_url,
                "project_root_path": str(cfg.project_root.resolve()),
                "media_dir_path": str(cfg.media_dir().resolve()),
                "export_dir_path": str(cfg.resolve_export_dir().resolve()),
                "notice": notice,
                "error": error,
            },
        )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "stats": db.dashboard_stats()}

    @app.get("/api/meta/status")
    async def api_meta_status() -> dict[str, Any]:
        """Return non-secret Meta Graph API connection health report."""
        client = InstagramGraphClient(cfg)
        return await client.check_connection_health()

    @app.get("/api/account-intelligence")
    def api_account_intelligence(brand: str = "") -> dict[str, Any]:
        slug = brand.strip() or cfg.smai_default_brand
        return {
            "latest_audit": db.get_latest_account_audit(slug),
            "snapshot": db.get_latest_account_snapshot(brand_slug=slug),
            "media_cache": db.list_account_media_cache(limit=20),
        }

    @app.get("/api/insights")
    def api_insights() -> dict[str, Any]:
        pipeline = SmaiPipeline(cfg, db)
        return pipeline.analyst.get_performance_breakdown(limit=100)

    @app.get("/api/experiments")
    def api_experiments() -> dict[str, Any]:
        return {
            "experiments": db.list_experiments(limit=30),
            "learnings": db.list_experiment_learnings(limit=30),
        }

    @app.get("/api/drafts")
    def api_list_drafts() -> list[dict[str, Any]]:
        return [_enrich_draft(d) for d in db.list_drafts(limit=50)]

    @app.post("/actions/analyze-account")
    async def action_analyze_account(
        handle_or_url: str = Form(...),
        niche_hint: str = Form(""),
        bio_hint: str = Form(""),
        ig_user_id: str = Form(""),
        ig_access_token: str = Form(""),
        gemini_api_key: str = Form(""),
        brand_slug: str = Form("default"),
    ) -> RedirectResponse:
        slug = brand_slug.strip() or cfg.smai_default_brand
        if gemini_api_key.strip():
            cfg.gemini_api_key = gemini_api_key.strip()
            cfg.smai_llm_provider = "gemini"
        analyzer = InstagramAccountAnalyzer(cfg, db)
        report = await analyzer.analyze(
            handle_or_url=handle_or_url,
            bio_hint=bio_hint,
            niche_hint=niche_hint,
            ig_user_id=ig_user_id,
            ig_access_token=ig_access_token,
            enable_network=cfg.smai_enable_network_scout,
        )
        analyzer.apply_to_brand(
            report=report,
            brand_slug=slug,
            ig_user_id=ig_user_id,
            ig_access_token=ig_access_token,
            gemini_api_key=gemini_api_key,
        )
        pipeline = SmaiPipeline(cfg, db)
        await pipeline.run_scout(brand_slug=slug, limit=5)
        return RedirectResponse(url=f"/?brand={slug}", status_code=303)

    @app.post("/actions/scout")
    async def action_scout(brand_slug: str = Form("")) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        slug = brand_slug.strip() or None
        await pipeline.run_scout(brand_slug=slug, limit=5)
        return RedirectResponse(url=f"/?brand={slug}" if slug else "/", status_code=303)

    @app.post("/actions/cycle")
    async def action_full_cycle(
        brand_slug: str = Form(""),
        cycle_mode: CycleMode = Form("draft_only"),
        approve_qa_passed: str = Form("0"),
    ) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        slug = brand_slug.strip() or None
        results = await pipeline.run_full_draft_cycle(
            brand_slug=slug,
            render_media=True,
            cycle_mode=cycle_mode,
            approve_qa_passed=(approve_qa_passed == "1"),
        )
        return _redirect_home(
            brand=slug or "",
            notice=f"Generated {len(results)} draft(s) via mode={cycle_mode}.",
        )

    @app.post("/actions/insights-sync")
    async def action_insights_sync(format: str = "") -> Any:
        if not cfg.has_meta_credentials():
            msg = "Cannot sync Insights: IG_ACCESS_TOKEN and IG_USER_ID are not configured."
            if format == "json":
                return JSONResponse(status_code=400, content={"ok": False, "error": msg})
            return _redirect_home(error=msg)
        client = InstagramGraphClient(cfg)
        pipeline = SmaiPipeline(cfg, db, graph_client=client)
        acct = await client.fetch_account_insights_safe(window_days=1)
        db.insert_account_insight(acct)
        media_captured = 0
        for row in db.list_publish_logs(limit=15):
            if row.get("outcome") != "published" or not row.get("ig_media_id"):
                continue
            fmt = str(row.get("draft_format") or "").lower()
            prod = "REELS" if fmt in ("reel", "slide_video") else ("STORY" if fmt == "story" else "FEED")
            try:
                snap = await client.fetch_media_insights_safe(
                    str(row["ig_media_id"]), media_product_type=prod, horizon_hours=24
                )
                pipeline.analyst.record_snapshot(int(row["id"]), snap)
                media_captured += 1
            except MetaAPIError:
                continue
        if format == "json":
            return {"ok": True, "media_synced": media_captured, "account": acct.model_dump()}
        return _redirect_home(notice=f"Synced Meta Insights: account + {media_captured} media item(s).")

    @app.post("/actions/generate")
    async def action_generate(
        fmt: ContentFormat = Form("carousel"),
        brand_slug: str = Form(""),
        topic_id: str = Form(""),
        custom_topic: str = Form(""),
    ) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        slug = brand_slug.strip() or None
        tid = int(topic_id) if topic_id.strip().isdigit() else None
        await pipeline.generate_draft(
            fmt=fmt,
            brand_slug=slug,
            topic_id=tid,
            custom_topic=custom_topic.strip() or None,
            render_media=True,
        )
        return RedirectResponse(url=f"/?brand={slug}" if slug else "/", status_code=303)

    @app.post("/drafts/{draft_id}/hook")
    def action_select_hook(draft_id: int, hook_idx: int = Form(0)) -> RedirectResponse:
        draft = db.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        db.update_draft_content(draft_id, selected_hook_idx=hook_idx)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/drafts/{draft_id}/caption")
    def action_update_caption(draft_id: int, caption: str = Form(...)) -> RedirectResponse:
        draft = db.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        db.update_draft_content(draft_id, caption=caption)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/drafts/{draft_id}/approve")
    def action_approve(draft_id: int) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        try:
            pipeline.approve_draft(draft_id)
        except Exception as exc:
            raise _raise_http(exc) from exc
        return _redirect_home(notice=f"Draft #{draft_id} approved.")

    @app.post("/drafts/{draft_id}/reject")
    def action_reject(draft_id: int) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        try:
            pipeline.reject_draft(draft_id)
        except Exception as exc:
            raise _raise_http(exc) from exc
        return _redirect_home(notice=f"Draft #{draft_id} rejected.")

    @app.post("/drafts/{draft_id}/reopen")
    def action_reopen(
        draft_id: int, target_status: str = Form("pending_approval")
    ) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        try:
            pipeline.reopen_draft(draft_id, target_status=target_status)
        except Exception as exc:
            raise _raise_http(exc) from exc
        return _redirect_home(notice=f"Draft #{draft_id} moved to '{target_status}'.")

    @app.post("/drafts/{draft_id}/export")
    async def action_export(draft_id: int) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        try:
            res = await pipeline.export_draft(draft_id)
        except Exception as exc:
            raise _raise_http(exc) from exc
        return _redirect_home(notice=f"Draft #{draft_id} exported to {res['export_path']}")

    @app.post("/drafts/{draft_id}/schedule")
    def action_schedule(draft_id: int, scheduled_for: str = Form("")) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        try:
            res = pipeline.schedule_draft(draft_id, scheduled_for=scheduled_for.strip() or None)
        except Exception as exc:
            raise _raise_http(exc) from exc
        return _redirect_home(notice=f"Draft #{draft_id} scheduled for {res['scheduled_for']}.")

    @app.post("/schedules/{schedule_id}/cancel")
    def action_cancel_schedule(schedule_id: int) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        try:
            res = pipeline.cancel_schedule(schedule_id)
        except Exception as exc:
            raise _raise_http(exc) from exc
        return _redirect_home(notice=f"Schedule #{schedule_id} (draft #{res['draft_id']}) cancelled.")

    @app.post("/actions/ig-browser-login")
    async def action_ig_browser_login() -> RedirectResponse:
        automator = InstagramBrowserAutomator(cfg)
        await automator.launch_login_window(wait_seconds=90)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/drafts/{draft_id}/stage-browser")
    async def action_stage_browser(draft_id: int) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        try:
            await pipeline.stage_in_browser(draft_id)
        except Exception as exc:
            raise _raise_http(exc) from exc
        return _redirect_home(
            notice=f"Draft #{draft_id} staged in IG Browser composer. Confirm after manually clicking Share."
        )

    @app.post("/drafts/{draft_id}/confirm-published")
    def action_confirm_published(
        draft_id: int,
        ig_media_id: str = Form(""),
        permalink: str = Form(""),
    ) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        try:
            pipeline.confirm_staged_publish(
                draft_id,
                ig_media_id=ig_media_id.strip(),
                permalink=permalink.strip(),
            )
        except Exception as exc:
            raise _raise_http(exc) from exc
        return _redirect_home(notice=f"Draft #{draft_id} confirmed as published.")

    @app.post("/drafts/{draft_id}/ig-browser")
    async def action_publish_ig_browser(
        draft_id: int,
        auto_share: str = Form("0"),
    ) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        try:
            if auto_share == "1":
                res = await pipeline.publish_draft(draft_id, mode="browser_share")
                return _redirect_home(notice=f"Draft #{draft_id} status: {res.get('status')}.")
            await pipeline.stage_in_browser(draft_id)
            return _redirect_home(
                notice=f"Draft #{draft_id} staged in IG Browser (awaiting manual Share confirmation)."
            )
        except Exception as exc:
            raise _raise_http(exc) from exc

    @app.post("/drafts/{draft_id}/publish")
    async def action_publish(draft_id: int, mode: str = Form("")) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        try:
            res = await pipeline.publish_draft(draft_id, mode=mode.strip() or None)
        except Exception as exc:
            raise _raise_http(exc) from exc
        return _redirect_home(
            notice=f"Draft #{draft_id} published ({res.get('mode')}, media_id={res.get('ig_media_id', '')})."
        )

    @app.post("/insights/record")
    def action_record_insight(
        publish_log_id: int = Form(...),
        horizon_hours: int = Form(24),
        reach: str = Form(""),
        plays: str = Form(""),
        likes: str = Form(""),
        comments: str = Form(""),
        saves: str = Form(""),
        shares: str = Form(""),
    ) -> RedirectResponse:
        def _opt_int(val: str) -> int | None:
            s = val.strip()
            return int(s) if s else None

        pipeline = SmaiPipeline(cfg, db)
        snap = InsightSnapshot(
            horizon_hours=horizon_hours,
            reach=_opt_int(reach),
            plays=_opt_int(plays),
            likes=_opt_int(likes),
            comments=_opt_int(comments),
            saves=_opt_int(saves),
            shares=_opt_int(shares),
            source="manual",
        )
        pipeline.analyst.record_snapshot(publish_log_id, snap)
        return _redirect_home(notice=f"Recorded insight snapshot for publish log #{publish_log_id}.")

    @app.post("/experiments/create")
    def action_create_experiment(
        name: str = Form(...),
        hypothesis: str = Form(...),
        variable_name: str = Form(...),
        control_value: str = Form(...),
        treatment_value: str = Form(...),
        primary_metric: str = Form("engagement_score"),
        min_sample_per_arm: int = Form(5),
    ) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        exp_id = pipeline.analyst.create_experiment(
            name=name.strip(),
            hypothesis=hypothesis.strip(),
            variable_name=variable_name.strip(),
            control_value=control_value.strip(),
            treatment_value=treatment_value.strip(),
            primary_metric=primary_metric.strip() or "engagement_score",
            min_sample_per_arm=min_sample_per_arm,
        )
        return _redirect_home(notice=f"Created experiment #{exp_id} ({name.strip()}).")

    @app.post("/experiments/{experiment_id}/assign")
    def action_assign_experiment(
        experiment_id: int,
        draft_id: int = Form(...),
        arm: str = Form(...),
    ) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        try:
            pipeline.analyst.assign_draft(experiment_id, draft_id, arm)
        except Exception as exc:
            raise _raise_http(exc) from exc
        return _redirect_home(notice=f"Assigned draft #{draft_id} to {arm} in experiment #{experiment_id}.")

    @app.post("/experiments/{experiment_id}/evaluate")
    def action_evaluate_experiment(
        experiment_id: int,
        horizon_hours: int = Form(24),
    ) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        try:
            res = pipeline.analyst.evaluate_experiment(experiment_id, horizon_hours=horizon_hours)
        except Exception as exc:
            raise _raise_http(exc) from exc
        return _redirect_home(
            notice=(
                f"Evaluated experiment #{experiment_id}: winner={res['winner']} "
                f"(confidence={res['confidence']})."
            )
        )

    @app.post("/experiment-learnings/{learning_id}/approve")
    def action_approve_learning(learning_id: int) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        try:
            pipeline.analyst.promote_learning(learning_id)
        except Exception as exc:
            raise _raise_http(exc) from exc
        return _redirect_home(notice=f"Experiment learning #{learning_id} approved for prompt feedback.")

    @app.post("/experiment-learnings/{learning_id}/reject")
    def action_reject_learning(learning_id: int) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        try:
            pipeline.analyst.reject_learning(learning_id)
        except Exception as exc:
            raise _raise_http(exc) from exc
        return _redirect_home(notice=f"Experiment learning #{learning_id} rejected.")

    return app
