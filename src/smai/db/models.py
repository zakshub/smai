"""Pydantic domain models for SMAI agents, media, and publishing."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


ContentFormat = Literal["carousel", "reel", "slide_video", "single_pic", "text_post", "story"]
ApprovalStatus = Literal["pending", "approved", "rejected", "published"]
QAStatus = Literal["pending", "pass", "revise"]


class TopicCandidate(BaseModel):
    title: str
    angle: str
    pillar: str
    viral_score: float = Field(ge=0.0, le=10.0, default=7.5)
    source_url: str = ""
    rationale: str = ""
    suggested_format: ContentFormat = "carousel"
    target_audience: str = ""
    recommended_time: str = ""
    algorithm_rationale: str = ""


class CarouselSlide(BaseModel):
    index: int
    role: Literal["hook", "context", "value", "summary", "cta"] = "value"
    eyebrow: str = ""
    headline: str
    body: str = ""
    bullets: list[str] = Field(default_factory=list)
    accent_stat: str = ""
    visual_url: str = ""


class ReelScene(BaseModel):
    index: int
    visual_query: str
    narration: str
    on_screen_text: str = ""
    duration_hint_sec: float = 4.0


class StoryFrame(BaseModel):
    index: int
    headline: str
    body: str = ""
    interactive_sticker: str = ""


class ABHookVariant(BaseModel):
    label: str
    hook_text: str
    angle: str = ""


class ContentPackage(BaseModel):
    format: ContentFormat
    title: str
    hook: str
    ab_hooks: list[ABHookVariant] = Field(default_factory=list)
    slides: list[CarouselSlide] = Field(default_factory=list)
    scenes: list[ReelScene] = Field(default_factory=list)
    story_frames: list[StoryFrame] = Field(default_factory=list)
    narration_script: str = ""
    text_body: str = ""
    target_audience: str = ""
    recommended_time: str = ""
    algorithm_rationale: str = ""
    caption: str
    hashtags: list[str] = Field(default_factory=list)
    cta: str = ""
    theme: str = "editorial-dark"


class QACheck(BaseModel):
    name: str
    passed: bool
    detail: str


class QAReport(BaseModel):
    status: QAStatus
    score: float = Field(ge=0.0, le=100.0)
    checks: list[QACheck] = Field(default_factory=list)
    revision_instructions: list[str] = Field(default_factory=list)


class WordTimestamp(BaseModel):
    word: str
    start_sec: float
    end_sec: float


class InsightSnapshot(BaseModel):
    horizon_hours: int = 24
    reach: int = 0
    plays: int = 0
    likes: int = 0
    comments: int = 0
    saves: int = 0
    shares: int = 0

    @property
    def engagement_score(self) -> float:
        """Weighted toward saves and shares, the strongest IG algorithmic signals."""
        denom = max(self.reach, 1)
        weighted = (
            self.likes * 1.0
            + self.comments * 2.5
            + self.saves * 5.0
            + self.shares * 6.0
        )
        return round((weighted / denom) * 100.0, 2)
