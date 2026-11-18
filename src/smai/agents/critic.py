"""Critic / QA Agent: validates copy, brand safety, slide density, and media specs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from smai.config import BrandBrain
from smai.db.models import ContentPackage, QACheck, QAReport


class CriticAgent:
    """Deterministic + policy QA gate before drafts enter the human approval queue."""

    def evaluate(
        self,
        brand: BrandBrain,
        package: ContentPackage,
        rendered_assets: list[dict[str, Any]] | None = None,
    ) -> QAReport:
        checks: list[QACheck] = []
        revisions: list[str] = []

        # 1. Banned phrase scan
        all_text = " ".join(
            [
                package.title,
                package.hook,
                package.caption,
                package.narration_script,
                package.text_body,
                *(f"{s.headline} {s.body}" for s in package.slides),
                *(s.narration for s in package.scenes),
            ]
        ).lower()
        found_banned = [p for p in brand.banned_phrases if p.lower() in all_text]
        checks.append(
            QACheck(
                name="banned_phrases",
                passed=len(found_banned) == 0,
                detail="No banned phrases found." if not found_banned else f"Remove banned phrases: {found_banned}",
            )
        )
        if found_banned:
            revisions.append(f"Remove banned phrases: {', '.join(found_banned)}")

        # 2. Hook length & punchiness
        hook_words = len(package.hook.split())
        hook_ok = 4 <= hook_words <= 24
        checks.append(
            QACheck(
                name="hook_punchiness",
                passed=hook_ok,
                detail=f"Hook has {hook_words} words (target 4-24).",
            )
        )
        if not hook_ok:
            revisions.append("Tighten the opening hook to 4-24 punchy words.")

        # 3. Format-specific structural checks
        if package.format == "carousel":
            slide_count_ok = 4 <= len(package.slides) <= 10
            checks.append(
                QACheck(
                    name="carousel_slide_count",
                    passed=slide_count_ok,
                    detail=f"{len(package.slides)} slides generated (target 4-10).",
                )
            )
            if not slide_count_ok:
                revisions.append("Adjust carousel to 5-8 slides.")

            max_words = brand.tone.max_words_per_slide
            overweight = [
                s.index
                for s in package.slides
                if len(f"{s.headline} {s.body}".split()) > max_words + 12
            ]
            density_ok = len(overweight) == 0
            checks.append(
                QACheck(
                    name="slide_word_density",
                    passed=density_ok,
                    detail=(
                        f"All slides within readable word budget (~{max_words}w)."
                        if density_ok
                        else f"Slides {overweight} exceed word density limit."
                    ),
                )
            )
            if not density_ok:
                revisions.append(f"Shorten copy on slides {overweight} under {max_words} words.")

        elif package.format == "single_pic":
            pic_ok = len(package.slides) >= 1 and len(package.slides[0].headline.split()) <= 20
            checks.append(
                QACheck(
                    name="single_pic_clarity",
                    passed=pic_ok,
                    detail="Single image card has punchy headline." if pic_ok else "Keep single-pic headline under 20 words.",
                )
            )
            if not pic_ok:
                revisions.append("Keep single-pic headline crisp and under 20 words.")

        elif package.format == "text_post":
            text_len = len((package.text_body or package.caption).split())
            text_ok = text_len >= 20
            checks.append(
                QACheck(
                    name="text_post_depth",
                    passed=text_ok,
                    detail=f"Text post has {text_len} words (target >= 20).",
                )
            )
            if not text_ok:
                revisions.append("Expand text post with at least 3 concrete bullet takeaways.")

        elif package.format in ("reel", "slide_video"):
            word_count = len(package.narration_script.split())
            est_seconds = round(word_count / 2.5, 1)
            duration_ok = 10 <= word_count <= int(brand.tone.max_reel_seconds * 2.8)
            checks.append(
                QACheck(
                    name="reel_pacing",
                    passed=duration_ok,
                    detail=f"Narration is {word_count} words (~{est_seconds}s).",
                )
            )
            if not duration_ok:
                revisions.append(
                    f"Keep Reel narration between 25 and {int(brand.tone.max_reel_seconds * 2.5)} words."
                )

            scenes_ok = len(package.scenes) >= 3
            checks.append(
                QACheck(
                    name="reel_scene_variety",
                    passed=scenes_ok,
                    detail=f"{len(package.scenes)} visual scenes defined.",
                )
            )
            if not scenes_ok:
                revisions.append("Split the Reel into at least 3 distinct visual scenes.")

        # 4. A/B hook check
        ab_ok = len(package.ab_hooks) >= 2
        checks.append(
            QACheck(
                name="ab_hooks_present",
                passed=ab_ok,
                detail=f"{len(package.ab_hooks)} A/B hook variants available.",
            )
        )

        # 5. Rendered asset verification if assets were passed
        if rendered_assets is not None:
            existing_files = [
                a for a in rendered_assets if Path(a["local_path"]).exists() and Path(a["local_path"]).stat().st_size > 0
            ]
            assets_ok = len(existing_files) > 0
            checks.append(
                QACheck(
                    name="rendered_media_exists",
                    passed=assets_ok,
                    detail=f"{len(existing_files)}/{len(rendered_assets)} rendered media files verified on disk.",
                )
            )
            if not assets_ok:
                revisions.append("Rendered media output missing or empty.")

        passed_count = sum(1 for c in checks if c.passed)
        score = round((passed_count / max(len(checks), 1)) * 100.0, 1)
        status = "pass" if all(c.passed for c in checks) else "revise"
        return QAReport(
            status=status,
            score=score,
            checks=checks,
            revision_instructions=revisions,
        )
