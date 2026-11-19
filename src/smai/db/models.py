"""Pydantic domain models for SMAI agents, media, and publishing."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


ContentFormat = Literal["carousel", "reel", "slide_video", "single_pic", "text_post", "story"]
ApprovalStatus = Literal[
    "draft",
    "pending_approval",
    "rejected",
    "approved",
    "exported",
    "scheduled",
    "staged",
    "publishing",
    "published",
    "failed",
]
QAStatus = Literal["pending", "pass", "revise"]
ProvenanceTier = Literal["verified_observed", "inferred_heuristic", "ai_recommended"]
ConfidenceLevel = Literal["high", "medium", "low", "none"]
CycleMode = Literal["draft_only", "export_review", "auto_schedule"]
ExperimentStatus = Literal["draft", "active", "completed", "cancelled"]

ALLOWED_DRAFT_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"pending_approval", "approved", "rejected"}),
    "pending_approval": frozenset({"approved", "rejected", "draft"}),
    "rejected": frozenset({"draft", "pending_approval"}),
    "approved": frozenset({"exported", "scheduled", "staged", "publishing", "rejected", "draft"}),
    "exported": frozenset({"scheduled", "staged", "publishing", "approved", "rejected", "draft"}),
    "scheduled": frozenset({"publishing", "approved", "exported", "staged", "rejected", "draft", "failed"}),
    "staged": frozenset({"publishing", "published", "approved", "failed", "draft"}),
    "publishing": frozenset({"published", "failed", "approved", "exported", "scheduled", "staged"}),
    "published": frozenset(),  # terminal state: never allow republishing or regression
    "failed": frozenset({"draft", "pending_approval", "approved", "exported", "scheduled", "staged", "publishing"}),
}


def normalize_approval_status(status: str) -> str:
    """Normalize legacy approval status values into the V2 state machine."""
    cleaned = (status or "draft").strip().lower()
    if cleaned == "pending":
        return "pending_approval"
    return cleaned


def can_transition_status(current: str, target: str) -> bool:
    """Return True when transitioning from current -> target is permitted by ALLOWED_DRAFT_TRANSITIONS."""
    cur = normalize_approval_status(current)
    nxt = normalize_approval_status(target)
    if cur == nxt and cur != "published":
        return True
    return nxt in ALLOWED_DRAFT_TRANSITIONS.get(cur, frozenset())


class DuplicatePublishError(RuntimeError):
    """Raised when attempting to republish a draft that is already published or in progress."""


class ApprovalRequiredError(RuntimeError):
    """Raised when attempting to export, schedule, stage, or publish an unapproved draft."""


class InvalidStateTransitionError(RuntimeError):
    """Raised when attempting an illegal draft lifecycle state transition."""


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
    reach: int | None = None
    views: int | None = None
    plays: int | None = None
    likes: int | None = None
    comments: int | None = None
    saves: int | None = None
    shares: int | None = None
    profile_visits: int | None = None
    follows: int | None = None
    avg_watch_time_ms: int | None = None
    source: str = "manual"
    raw_metrics: dict[str, int | float | str | None] = {}
    unsupported_metrics: list[str] = []

    @property
    def save_rate(self) -> float | None:
        if self.reach is None or self.reach <= 0 or self.saves is None:
            return None
        return round((self.saves / self.reach) * 100.0, 2)

    @property
    def share_rate(self) -> float | None:
        if self.reach is None or self.reach <= 0 or self.shares is None:
            return None
        return round((self.shares / self.reach) * 100.0, 2)

    @property
    def engagement_score(self) -> float | None:
        """Weighted toward saves and shares; returns None if reach/views or engagement metrics are absent."""
        denom = (
            self.reach
            if (self.reach is not None and self.reach > 0)
            else (self.views if (self.views is not None and self.views > 0) else None)
        )
        observed_counts = [self.likes, self.comments, self.saves, self.shares]
        if denom is None or all(v is None for v in observed_counts):
            return None
        weighted = (
            (self.likes or 0) * 1.0
            + (self.comments or 0) * 2.5
            + (self.saves or 0) * 5.0
            + (self.shares or 0) * 6.0
        )
        return round((weighted / denom) * 100.0, 2)


class AccountInsightResult(BaseModel):
    """Account-level Meta Insights snapshot preserving raw responses and unsupported metrics."""

    ig_user_id: str = ""
    window_days: int = 1
    captured_at: str = ""
    source: str = "meta_graph_api_v27"
    reach: int | None = None
    views: int | None = None
    profile_views: int | None = None
    accounts_engaged: int | None = None
    total_interactions: int | None = None
    follower_delta: int | None = None
    raw_metrics: dict[str, int | float | str | None] = {}
    unsupported_metrics: list[str] = []
    metric_errors: dict[str, str] = {}
