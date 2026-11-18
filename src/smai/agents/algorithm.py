"""Instagram Algorithm & Audience Timing Strategist."""

from __future__ import annotations

from dataclasses import dataclass

from smai.config import AudienceSegment, BrandBrain
from smai.db.models import ContentFormat


ALGORITHM_FORMAT_PLAYBOOK: dict[ContentFormat, dict[str, str]] = {
    "reel": {
        "primary_signal": "3s hook hold rate + avg watch time + DM sends per reach",
        "distribution": "Non-follower Explore & Reels discovery engine",
        "rule": "Open with a 3-second pattern interrupt, keep pace under 45s, and burn high-contrast pop captions.",
    },
    "slide_video": {
        "primary_signal": "100% completion rate + replays + high saves on educational motion slides",
        "distribution": "Reels tab + high-retention educational feeds",
        "rule": "Animate spring-eased kinetic text cards with progress bars, step bullets, and lo-fi BGM under voiceover.",
    },
    "carousel": {
        "primary_signal": "Swipe-through completion + saves per reach",
        "distribution": "Follower feed + high-save Explore resurgence",
        "rule": "Slide 1 promises a concrete payoff; slides 2-5 deliver skimmable steps; final slide drives saves.",
    },
    "single_pic": {
        "primary_signal": "Shares to Stories + quick saves",
        "distribution": "Feed + profile aesthetic anchor",
        "rule": "One bold contrarian insight or visual framework readable in under 3 seconds.",
    },
    "text_post": {
        "primary_signal": "Caption dwell time + comment debate + profile visits",
        "distribution": "High-intent core followers & thought-leadership threads",
        "rule": "Pair a clean minimalist typography card with a structured mini-essay caption and debate prompt.",
    },
    "story": {
        "primary_signal": "Sticker taps (Poll/Slider/Quiz) + direct DM replies",
        "distribution": "Warm follower retention & algorithm relationship boost",
        "rule": "Use interactive stickers on frame 1-2 to trigger immediate engagement signals.",
    },
}


@dataclass
class StrategyRecommendation:
    suggested_format: ContentFormat
    target_audience: str
    recommended_time: str
    algorithm_rationale: str


class IGAlgorithmStrategist:
    """Matches topics to Instagram algorithm signals, audience segments, and regular time windows."""

    def recommend(
        self,
        brand: BrandBrain,
        title: str,
        index: int = 0,
        preferred_format: ContentFormat | None = None,
    ) -> StrategyRecommendation:
        lower = title.lower()
        segments = brand.audience_segments or [
            AudienceSegment(
                name=brand.audience[:60] or "Core Niche Audience",
                preferred_formats=["carousel", "reel", "slide_video"],
                peak_hours=brand.schedule_windows[0] if brand.schedule_windows else [19, 22],
            )
        ]
        segment = segments[index % len(segments)]

        if preferred_format:
            fmt: ContentFormat = preferred_format
        elif any(k in lower for k in ("step", "checklist", "system", "audit", "guide", "framework")):
            fmt = "carousel" if index % 2 == 0 else "slide_video"
        elif any(k in lower for k in ("why", "stop", "mistake", "fails", "secret")):
            fmt = "reel"
        elif any(k in lower for k in ("prompt", "tool", "habit", "rule")):
            fmt = "single_pic"
        elif any(k in lower for k in ("essay", "opinion", "thread", "story", "lesson")):
            fmt = "text_post"
        else:
            raw_pref = segment.preferred_formats[0] if segment.preferred_formats else "carousel"
            fmt = raw_pref if raw_pref in ALGORITHM_FORMAT_PLAYBOOK else "carousel"  # type: ignore[assignment]

        peak = segment.peak_hours if len(segment.peak_hours) == 2 else [19, 22]
        recommended_time = f"{peak[0]:02d}:00-{peak[1]:02d}:00 {brand.timezone} (Regular Peak)"
        playbook = ALGORITHM_FORMAT_PLAYBOOK[fmt]
        rationale = (
            f"[{fmt.upper()}] Optimizes for {playbook['primary_signal']} "
            f"via {playbook['distribution']}. Target: {segment.name}."
        )
        return StrategyRecommendation(
            suggested_format=fmt,
            target_audience=segment.name,
            recommended_time=recommended_time,
            algorithm_rationale=rationale,
        )

    def playbook_summary(self) -> str:
        lines = ["Instagram Algorithm Ranking Rules:"]
        for fmt, info in ALGORITHM_FORMAT_PLAYBOOK.items():
            lines.append(f"- {fmt}: {info['primary_signal']} ({info['rule']})")
        return "\n".join(lines)
