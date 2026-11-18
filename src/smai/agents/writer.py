"""Writer Agent: generates topic-specific Carousels, Reels, Slide-Videos, Single Pics, Text Posts, and Stories."""

from __future__ import annotations

import hashlib
import re

from smai.agents.algorithm import IGAlgorithmStrategist
from smai.agents.base import LLMClient
from smai.config import BrandBrain
from smai.db.models import (
    ABHookVariant,
    CarouselSlide,
    ContentFormat,
    ContentPackage,
    ReelScene,
    StoryFrame,
    TopicCandidate,
)


def _extract_subject(title: str) -> str:
    cleaned = re.sub(r"^How builders can apply:\s*", "", title, flags=re.IGNORECASE).strip()
    return cleaned or title


class WriterAgent:
    """Produces faceless Instagram content packages across all text, pic, slide-video, and reel formats."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm
        self.strategist = IGAlgorithmStrategist()

    async def write(
        self,
        brand: BrandBrain,
        topic: TopicCandidate,
        fmt: ContentFormat = "carousel",
        insights_summary: str = "",
        revision_notes: list[str] | None = None,
        draft_id: int | None = None,
    ) -> ContentPackage:
        cta = brand.cta_bank[0] if brand.cta_bank else "Save this breakdown for later."
        rec = self.strategist.recommend(brand, topic.title, preferred_format=fmt)
        target_audience = topic.target_audience or rec.target_audience
        recommended_time = topic.recommended_time or rec.recommended_time
        algorithm_rationale = topic.algorithm_rationale or rec.algorithm_rationale

        system_prompt = (
            "You are an expert faceless Instagram copywriter and visual director. "
            "Output strictly valid JSON for the ContentPackage schema: "
            "{format, title, hook, ab_hooks:[{label,hook_text,angle}], "
            "slides:[{index,role,eyebrow,headline,body,bullets,accent_stat}], "
            "scenes:[{index,visual_query,narration,on_screen_text,duration_hint_sec}], "
            "story_frames:[{index,headline,body,interactive_sticker}], "
            "narration_script, text_body, target_audience, recommended_time, "
            "algorithm_rationale, caption, hashtags, cta, theme}. "
            f"Keep each slide under {brand.tone.max_words_per_slide} words. "
            f"Keep reel narration under {brand.tone.max_reel_seconds} seconds (~100 words). "
            f"NEVER use these phrases: {brand.banned_phrases}."
        )
        user_prompt = (
            f"Format: {fmt}\n"
            f"Brand: {brand.name} ({brand.handle})\n"
            f"Voice: {brand.tone.voice}\n"
            f"Theme: {brand.visual_style.carousel_theme}\n"
            f"Topic: {topic.title}\n"
            f"Angle: {topic.angle}\n"
            f"Pillar: {topic.pillar}\n"
            f"Target Audience: {target_audience}\n"
            f"Recommended Time: {recommended_time}\n"
            f"Algorithm Strategy: {algorithm_rationale}\n"
            f"Preferred CTA: {cta}\n"
            f"Core Hashtags: {brand.hashtags.core}\n"
            f"Winning Pattern Notes: {insights_summary or 'Focus on clarity, saves, and sends.'}\n"
            f"Revision Notes (must fix if present): {revision_notes or []}"
        )

        def _fallback() -> ContentPackage:
            return self._deterministic_package(
                brand, topic, fmt, target_audience, recommended_time, algorithm_rationale
            )

        pkg = await self.llm.complete_json(
            agent_name="writer",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=ContentPackage,
            fallback_factory=_fallback,
            draft_id=draft_id,
        )
        pkg.format = fmt
        pkg.target_audience = pkg.target_audience or target_audience
        pkg.recommended_time = pkg.recommended_time or recommended_time
        pkg.algorithm_rationale = pkg.algorithm_rationale or algorithm_rationale
        if not pkg.theme:
            pkg.theme = brand.visual_style.carousel_theme
        if not pkg.hashtags:
            pkg.hashtags = list(brand.hashtags.core)
        return pkg

    def _deterministic_package(
        self,
        brand: BrandBrain,
        topic: TopicCandidate,
        fmt: ContentFormat,
        target_audience: str,
        recommended_time: str,
        algorithm_rationale: str,
    ) -> ContentPackage:
        subject = _extract_subject(topic.title)
        short_subj = subject[:48]
        digest = int(hashlib.md5(subject.encode("utf-8")).hexdigest()[:6], 16)
        cta = brand.cta_bank[digest % len(brand.cta_bank)] if brand.cta_bank else "Save this system for later."
        hashtags = list(brand.hashtags.core[:5]) or ["#automation", "#aitools", "#systems"]

        hook_a = f"Most operators lose 5+ hours weekly because they mismanage: {short_subj}"
        hook_b = f"Stop guessing on {short_subj}. Steal this 3-layer operating blueprint."
        ab_hooks = [
            ABHookVariant(label="A (Loss Aversion + Metric)", hook_text=hook_a, angle="Concrete time cost"),
            ABHookVariant(label="B (Contrarian Blueprint)", hook_text=hook_b, angle="Direct command"),
        ]

        if fmt == "carousel":
            slides = [
                CarouselSlide(
                    index=1,
                    role="hook",
                    eyebrow=topic.pillar.upper()[:28] or "ARCHITECTURAL BREAKDOWN",
                    headline=subject,
                    body=f"Built for {target_audience}: swipe for the 4-part execution blueprint.",
                    bullets=["Zero manual guesswork", "Plug-and-play daily workflow"],
                    accent_stat="5.4 HRS SAVED",
                ),
                CarouselSlide(
                    index=2,
                    role="context",
                    eyebrow="01 / THE HIDDEN TRAP",
                    headline=f"Why most fail at {short_subj[:32]}",
                    body="They stack new tools on top of an undocumented process instead of pruning friction first.",
                    bullets=["Too many manual handoffs", "No single source of truth"],
                    accent_stat="-42% DRAG",
                ),
                CarouselSlide(
                    index=3,
                    role="value",
                    eyebrow="02 / LAYER ONE: INTAKE",
                    headline="Filter signals before acting",
                    body=f"Route every trigger related to {short_subj[:28]} into one structured queue.",
                    bullets=["Batch triggers twice a day", "Drop low-leverage noise early"],
                    accent_stat="1 QUEUE",
                ),
                CarouselSlide(
                    index=4,
                    role="value",
                    eyebrow="03 / LAYER TWO: PROTOCOL",
                    headline="Lock an input-to-output checklist",
                    body="Define exact acceptance criteria so automation or AI produces usable drafts on pass one.",
                    bullets=["Strict JSON/template schema", "Automated QA pre-check"],
                    accent_stat="3.2X OUTPUT",
                ),
                CarouselSlide(
                    index=5,
                    role="value",
                    eyebrow="04 / LAYER THREE: REVIEW",
                    headline="10-minute human approval gate",
                    body="Never auto-publish blind. Spend 10 minutes reviewing scored drafts and picking winning hooks.",
                    bullets=["A/B hook comparison", "Peak-window auto-scheduling"],
                    accent_stat="10 MIN/DAY",
                ),
                CarouselSlide(
                    index=6,
                    role="cta",
                    eyebrow="BOOKMARK & DEPLOY",
                    headline=f"Turn {short_subj[:30]} into a weekly asset.",
                    body=cta,
                    bullets=[f"Follow {brand.handle}", "Share with a builder"],
                    accent_stat=brand.handle,
                ),
            ]
            caption = (
                f"{hook_a}\n\n"
                f"Topic: {subject}\n"
                f"Built for: {target_audience}\n\n"
                "Key takeaways:\n"
                "1. Prune broken steps before adding automation.\n"
                "2. Lock an input-output checklist with automated QA.\n"
                "3. Approve in one 10-minute daily window.\n\n"
                f"{cta}\n\n"
                + " ".join(hashtags)
            )
            return ContentPackage(
                format="carousel",
                title=topic.title,
                hook=hook_a,
                ab_hooks=ab_hooks,
                slides=slides,
                target_audience=target_audience,
                recommended_time=recommended_time,
                algorithm_rationale=algorithm_rationale,
                caption=caption,
                hashtags=hashtags,
                cta=cta,
                theme=brand.visual_style.carousel_theme,
            )

        if fmt in ("reel", "slide_video"):
            scenes = [
                ReelScene(
                    index=1,
                    visual_query=f"futuristic dark command center {short_subj}",
                    narration=hook_a,
                    on_screen_text=short_subj.upper()[:30],
                    duration_hint_sec=4.2,
                ),
                ReelScene(
                    index=2,
                    visual_query="minimal dark architect desk workflow blueprint",
                    narration=f"Step one: eliminate the manual friction around {short_subj[:32]} before you touch any automation tool.",
                    on_screen_text="01 / PRUNE FRICTION FIRST",
                    duration_hint_sec=5.0,
                ),
                ReelScene(
                    index=3,
                    visual_query="glowing neural network data pipeline dark minimal",
                    narration="Step two: lock a repeatable input-to-output template so every draft passes quality checks automatically.",
                    on_screen_text="02 / LOCK REPEATABLE TEMPLATE",
                    duration_hint_sec=5.0,
                ),
                ReelScene(
                    index=4,
                    visual_query="high contrast dashboard analytics metrics screen",
                    narration=f"Step three: batch review in ten minutes and schedule for peak audience hours. {cta}",
                    on_screen_text="03 / 10-MIN BATCH APPROVAL",
                    duration_hint_sec=5.0,
                ),
            ]
            narration_script = " ".join(s.narration for s in scenes)
            caption = (
                f"{hook_a}\n\n"
                f"{subject} — distilled into a 3-step motion breakdown for {target_audience}.\n\n"
                f"{cta}\n\n"
                + " ".join(hashtags)
            )
            return ContentPackage(
                format=fmt,
                title=topic.title,
                hook=hook_a,
                ab_hooks=ab_hooks,
                scenes=scenes,
                narration_script=narration_script,
                target_audience=target_audience,
                recommended_time=recommended_time,
                algorithm_rationale=algorithm_rationale,
                caption=caption,
                hashtags=hashtags,
                cta=cta,
                theme=brand.visual_style.carousel_theme,
            )

        if fmt == "single_pic":
            slides = [
                CarouselSlide(
                    index=1,
                    role="hook",
                    eyebrow="INFOGRAPHIC CHEATSHEET",
                    headline=subject,
                    body=f"The 3-step execution filter for {target_audience}:",
                    bullets=[
                        "1. Centralize triggers into one clean queue",
                        "2. Standardize with a strict QA checklist",
                        "3. Batch approve & schedule into peak windows",
                    ],
                    accent_stat="CHEATSHEET",
                )
            ]
            caption = (
                f"{hook_b}\n\n"
                f"One-glance cheatsheet on {subject} for {target_audience}.\n\n"
                f"{cta}\n\n"
                + " ".join(hashtags)
            )
            return ContentPackage(
                format="single_pic",
                title=topic.title,
                hook=hook_b,
                ab_hooks=ab_hooks,
                slides=slides,
                target_audience=target_audience,
                recommended_time=recommended_time,
                algorithm_rationale=algorithm_rationale,
                caption=caption,
                hashtags=hashtags,
                cta=cta,
                theme="bold-gradient",
            )

        if fmt == "text_post":
            text_body = (
                f"{hook_a}\n\n"
                f"When builders tackle '{subject}', they usually overcomplicate the stack.\n"
                "What actually compounds week after week:\n"
                "-> 1. Single intake queue (no scattered tabs)\n"
                "-> 2. Documented checklist per repeatable deliverable\n"
                "-> 3. 10-minute daily review ritual instead of constant reactive work\n\n"
                f"Question for {target_audience}: which bottleneck around {short_subj[:28]} slows you down most?\n\n"
                f"{cta}"
            )
            slides = [
                CarouselSlide(
                    index=1,
                    role="hook",
                    eyebrow="OPERATOR FIELD NOTE",
                    headline=subject,
                    body="Read the full micro-essay below & drop your #1 bottleneck in the comments.",
                    bullets=[
                        "Prune before automating",
                        "Checklists beat raw prompts",
                        "Batch review protects deep work",
                    ],
                    accent_stat="FIELD NOTE",
                )
            ]
            return ContentPackage(
                format="text_post",
                title=topic.title,
                hook=hook_a,
                ab_hooks=ab_hooks,
                slides=slides,
                text_body=text_body,
                target_audience=target_audience,
                recommended_time=recommended_time,
                algorithm_rationale=algorithm_rationale,
                caption=f"{text_body}\n\n" + " ".join(hashtags),
                hashtags=hashtags,
                cta=cta,
                theme="clean-light",
            )

        story_frames = [
            StoryFrame(
                index=1,
                headline=subject,
                body=f"Quick check for {target_audience}: is this automated in your stack yet?",
                interactive_sticker="POLL: Fully Automated / Still Manual",
            ),
            StoryFrame(
                index=2,
                headline="The 3-Layer Fix",
                body="1. Single Queue -> 2. Strict Checklist -> 3. 10-Min Batch Review.",
                interactive_sticker="SLIDER: Rate this system",
            ),
            StoryFrame(
                index=3,
                headline="Steal the full breakdown",
                body=cta,
                interactive_sticker="LINK: Latest Post in Feed",
            ),
        ]
        return ContentPackage(
            format="story",
            title=topic.title,
            hook=hook_a,
            ab_hooks=ab_hooks,
            story_frames=story_frames,
            target_audience=target_audience,
            recommended_time=recommended_time,
            algorithm_rationale=algorithm_rationale,
            caption=f"{hook_a}\n\n{cta}",
            hashtags=hashtags,
            cta=cta,
            theme=brand.visual_style.carousel_theme,
        )
