"""Configuration and BrandBrain loader for SMAI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_project_root() -> Path:
    return Path(__file__).resolve().parents[2]


class ToneConfig(BaseModel):
    voice: str = "Sharp, calm, editorial, concrete numbers over fluff"
    reading_level: str = "Grade 7-9 punchy sentences"
    max_words_per_slide: int = 28
    max_reel_seconds: int = 45


class VisualStyleConfig(BaseModel):
    carousel_theme: str = "editorial-dark"
    primary_color: str = "#0F172A"
    accent_color: str = "#38BDF8"
    text_color: str = "#F8FAFC"
    muted_color: str = "#94A3B8"
    font_family: str = "Inter, system-ui, sans-serif"
    watermark: str = "@quietleverage.ai"


class ScoutSourcesConfig(BaseModel):
    rss: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    subreddits: list[str] = Field(default_factory=list)
    seed_topics: list[str] = Field(default_factory=list)


class HashtagsConfig(BaseModel):
    core: list[str] = Field(default_factory=list)


class AudienceSegment(BaseModel):
    name: str
    pain_points: list[str] = Field(default_factory=list)
    preferred_formats: list[str] = Field(default_factory=lambda: ["carousel", "reel"])
    peak_hours: list[int] = Field(default_factory=lambda: [19, 22])


class RegularCadenceConfig(BaseModel):
    daily_mix: list[str] = Field(
        default_factory=lambda: ["reel", "carousel", "single_pic", "text_post", "story"]
    )
    posts_per_day: int = 2
    daily_draft_hour: int = 8
    auto_draft_only: bool = True


class BrandBrain(BaseModel):
    """Persistent niche, voice, visual, audience, and compliance identity for a faceless IG page."""

    slug: str
    name: str
    handle: str
    niche: str
    audience: str
    timezone: str = "Asia/Karachi"
    audience_segments: list[AudienceSegment] = Field(default_factory=list)
    regular_cadence: RegularCadenceConfig = Field(default_factory=RegularCadenceConfig)
    pillars: list[str] = Field(default_factory=list)
    tone: ToneConfig = Field(default_factory=ToneConfig)
    banned_phrases: list[str] = Field(default_factory=list)
    visual_style: VisualStyleConfig = Field(default_factory=VisualStyleConfig)
    cta_bank: list[str] = Field(default_factory=list)
    hashtags: HashtagsConfig = Field(default_factory=HashtagsConfig)
    scout_sources: ScoutSourcesConfig = Field(default_factory=ScoutSourcesConfig)
    schedule_windows: list[list[int]] = Field(
        default_factory=lambda: [[9, 11], [14, 16], [19, 22]]
    )


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables and .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    project_root: Path = Field(default_factory=_default_project_root)
    smai_timezone: str = "Asia/Karachi"
    smai_default_brand: str = "default"
    smai_data_dir: Path = Path("./data")
    smai_export_dir: Path = Path("./exports")
    smai_log_level: str = "INFO"
    smai_enable_network_scout: bool = True
    smai_prefer_playwright: bool = True

    # LLM
    smai_llm_provider: str = "mock"  # gemini | openai | anthropic | ollama | mock
    smai_llm_model: str = "gemini-2.5-flash"
    gemini_api_key: str = ""
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"

    # TTS
    smai_tts_provider: str = "edge"  # edge | elevenlabs | openai | silent
    smai_tts_voice: str = "en-US-ChristopherNeural"
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""

    # Stock media
    pexels_api_key: str = ""
    pixabay_api_key: str = ""

    # Publishing
    smai_publish_mode: str = "export"  # export | graph_api
    ig_user_id: str = ""
    ig_access_token: str = ""
    ig_graph_api_version: str = "v21.0"
    smai_public_media_base_url: str = ""
    s3_bucket: str = ""
    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    def resolve_data_dir(self) -> Path:
        path = self.smai_data_dir
        if not path.is_absolute():
            path = self.project_root / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve_export_dir(self) -> Path:
        path = self.smai_export_dir
        if not path.is_absolute():
            path = self.project_root / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    def db_path(self) -> Path:
        return self.resolve_data_dir() / "smai.db"

    def media_dir(self) -> Path:
        path = self.resolve_data_dir() / "media"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def brands_dir(self) -> Path:
        return self.project_root / "configs" / "brands"


def load_brand(slug: str, settings: Settings | None = None) -> BrandBrain:
    """Load a BrandBrain YAML file by slug."""
    cfg = settings or Settings()
    brand_path = cfg.brands_dir() / f"{slug}.yaml"
    if not brand_path.exists():
        raise FileNotFoundError(f"Brand configuration not found: {brand_path}")
    raw: dict[str, Any] = yaml.safe_load(brand_path.read_text(encoding="utf-8")) or {}
    return BrandBrain.model_validate(raw)


def list_brand_slugs(settings: Settings | None = None) -> list[str]:
    cfg = settings or Settings()
    brands_dir = cfg.brands_dir()
    if not brands_dir.exists():
        return []
    return sorted(p.stem for p in brands_dir.glob("*.yaml"))
