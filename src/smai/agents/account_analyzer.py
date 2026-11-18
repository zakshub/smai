"""Instagram Account Analyzer & BrandBrain Auto-Tuner."""

from __future__ import annotations

import html
import re
from typing import Any

import httpx
import yaml
from pydantic import BaseModel, Field

from smai.agents.base import LLMClient
from smai.config import AudienceSegment, BrandBrain, Settings, load_brand
from smai.db.database import Database


class AccountAuditReport(BaseModel):
    handle: str
    display_name: str = ""
    biography: str = ""
    followers_count: int = 0
    media_count: int = 0
    detected_niche: str
    audit_score: float = Field(ge=0.0, le=100.0, default=78.0)
    data_source: str = "profile_analysis"  # graph_api | public_meta | handle_inference
    bio_diagnosis: str
    algorithm_gaps: list[str] = Field(default_factory=list)
    format_strategy: dict[str, str] = Field(default_factory=dict)
    recommended_pillars: list[str] = Field(default_factory=list)
    recommended_keywords: list[str] = Field(default_factory=list)
    top_post_ideas: list[str] = Field(default_factory=list)
    recent_posts_analyzed: int = 0
    avg_likes: float = 0.0
    avg_comments: float = 0.0


def normalize_handle(raw: str) -> str:
    """Extract clean Instagram username from @handle or instagram.com URL."""
    text = raw.strip()
    m = re.search(r"instagram\.com/([A-Za-z0-9._]+)", text, re.IGNORECASE)
    if m:
        username = m.group(1)
    else:
        username = text.lstrip("@").split("/")[0].strip()
    username = re.sub(r"[^A-Za-z0-9._]", "", username)
    return username or "mybrand"


class InstagramAccountAnalyzer:
    """
    Analyzes an Instagram account (via Official Meta Graph API when tokens are set,
    or via public profile metadata + handle/bio intelligence) and auto-configures BrandBrain.
    """

    def __init__(self, settings: Settings, db: Database, llm: LLMClient | None = None) -> None:
        self.settings = settings
        self.db = db
        self.llm = llm or LLMClient(settings, db=db)

    async def analyze(
        self,
        handle_or_url: str,
        bio_hint: str = "",
        niche_hint: str = "",
        ig_user_id: str = "",
        ig_access_token: str = "",
        enable_network: bool = True,
    ) -> AccountAuditReport:
        username = normalize_handle(handle_or_url)
        handle = f"@{username}"

        user_id = ig_user_id.strip() or self.settings.ig_user_id.strip()
        token = ig_access_token.strip() or self.settings.ig_access_token.strip()

        profile_data: dict[str, Any] = {}
        recent_media: list[dict[str, Any]] = []
        data_source = "handle_inference"

        # 1. Try Official Meta Instagram Graph API if credentials are provided
        if enable_network and user_id and token:
            graph_prof, graph_media = await self._fetch_graph_api_profile(user_id, token)
            if graph_prof:
                profile_data = graph_prof
                recent_media = graph_media
                data_source = "graph_api"
                if graph_prof.get("username"):
                    username = str(graph_prof["username"])
                    handle = f"@{username}"

        # 2. Try public Instagram profile meta tag inspection if Graph API wasn't used
        if enable_network and not profile_data:
            pub_meta = await self._fetch_public_profile_meta(username)
            if pub_meta:
                profile_data = pub_meta
                data_source = "public_meta"

        biography = (
            str(profile_data.get("biography") or "").strip()
            or bio_hint.strip()
        )
        display_name = (
            str(profile_data.get("name") or "").strip()
            or username.replace(".", " ").replace("_", " ").title()
        )
        followers = int(profile_data.get("followers_count") or 0)
        media_count = int(profile_data.get("media_count") or len(recent_media))

        return self._build_audit_report(
            handle=handle,
            username=username,
            display_name=display_name,
            biography=biography,
            niche_hint=niche_hint.strip(),
            followers=followers,
            media_count=media_count,
            recent_media=recent_media,
            data_source=data_source,
        )

    def apply_to_brand(
        self,
        report: AccountAuditReport,
        brand_slug: str = "default",
        ig_user_id: str = "",
        ig_access_token: str = "",
        gemini_api_key: str = "",
    ) -> BrandBrain:
        """Update BrandBrain YAML + SQLite brand record from the account audit report."""
        try:
            brand = load_brand(brand_slug, self.settings)
        except Exception:
            brand = load_brand(self.settings.smai_default_brand, self.settings)

        brand.name = report.display_name or report.handle.lstrip("@").title()
        brand.handle = report.handle
        brand.niche = report.detected_niche
        brand.visual_style.watermark = report.handle

        if report.recommended_pillars:
            brand.pillars = report.recommended_pillars[:4]
        if report.recommended_keywords:
            brand.scout_sources.keywords = report.recommended_keywords[:4]
        if report.top_post_ideas:
            brand.scout_sources.seed_topics = report.top_post_ideas[:5]

        # Save updated YAML to configs/brands/<slug>.yaml
        brand_path = self.settings.brands_dir() / f"{brand.slug}.yaml"
        brand_path.parent.mkdir(parents=True, exist_ok=True)
        brand_path.write_text(
            yaml.safe_dump(brand.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

        # Persist in SQLite
        self.db.upsert_brand(brand, ig_user_id=ig_user_id or self.settings.ig_user_id)
        self.db.save_account_audit(brand.slug, report.model_dump_json())

        # If Graph API or Gemini credentials were supplied in the form, update .env safely
        if ig_user_id.strip() or ig_access_token.strip() or gemini_api_key.strip():
            self._update_env_credentials(
                ig_user_id.strip(), ig_access_token.strip(), gemini_api_key.strip()
            )
            if gemini_api_key.strip():
                self.settings.gemini_api_key = gemini_api_key.strip()
                self.settings.smai_llm_provider = "gemini"

        return brand

    async def _fetch_graph_api_profile(
        self, ig_user_id: str, access_token: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        base = f"https://graph.facebook.com/{self.settings.ig_graph_api_version}"
        timeout = httpx.Timeout(6.0, connect=3.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                prof_res = await client.get(
                    f"{base}/{ig_user_id}",
                    params={
                        "fields": "username,name,biography,followers_count,follows_count,media_count",
                        "access_token": access_token,
                    },
                )
                prof_res.raise_for_status()
                prof = prof_res.json()

                media_res = await client.get(
                    f"{base}/{ig_user_id}/media",
                    params={
                        "fields": "id,caption,media_type,timestamp,like_count,comments_count,permalink",
                        "limit": 15,
                        "access_token": access_token,
                    },
                )
                media_list = media_res.json().get("data", []) if media_res.status_code == 200 else []
                return prof, media_list
        except Exception:
            return {}, []

    async def _fetch_public_profile_meta(self, username: str) -> dict[str, Any]:
        """Attempt lightweight public meta tag inspection from instagram.com/<username>/."""
        url = f"https://www.instagram.com/{username}/"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            )
        }
        timeout = httpx.Timeout(3.5, connect=2.0)
        try:
            async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
                res = await client.get(url)
                if res.status_code != 200:
                    return {}
                text = res.text
                desc_match = re.search(
                    r'<meta\s+(?:property="og:description"|name="description")\s+content="([^"]+)"',
                    text,
                    re.IGNORECASE,
                )
                title_match = re.search(
                    r'<meta\s+property="og:title"\s+content="([^"]+)"',
                    text,
                    re.IGNORECASE,
                )
                if not desc_match and not title_match:
                    return {}
                desc = html.unescape(desc_match.group(1)) if desc_match else ""
                title = html.unescape(title_match.group(1)) if title_match else ""
                followers = 0
                posts = 0
                f_m = re.search(r"([\d,.]+)\s*([KMB])?\s*Followers", desc, re.IGNORECASE)
                if f_m:
                    followers = self._parse_human_int(f_m.group(1), f_m.group(2))
                p_m = re.search(r"([\d,.]+)\s*([KMB])?\s*Posts", desc, re.IGNORECASE)
                if p_m:
                    posts = self._parse_human_int(p_m.group(1), p_m.group(2))
                name_part = title.split("(")[0].strip() if "(" in title else username
                return {
                    "name": name_part[:60],
                    "biography": desc[:240],
                    "followers_count": followers,
                    "media_count": posts,
                }
        except Exception:
            return {}

    @staticmethod
    def _parse_human_int(num_str: str, suffix: str | None) -> int:
        try:
            val = float(num_str.replace(",", ""))
            mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get((suffix or "").upper(), 1)
            return int(val * mult)
        except Exception:
            return 0

    def _build_audit_report(
        self,
        handle: str,
        username: str,
        display_name: str,
        biography: str,
        niche_hint: str,
        followers: int,
        media_count: int,
        recent_media: list[dict[str, Any]],
        data_source: str,
    ) -> AccountAuditReport:
        combined_signal = f"{username} {display_name} {biography} {niche_hint}".lower()

        if any(k in combined_signal for k in ("fin", "invest", "money", "wealth", "crypto", "trading", "biz")):
            detected_niche = "Personal Finance, Wealth Systems & Smart Investing"
            pillars = [
                "Cashflow & compounding rules most earners ignore",
                "High-ROI digital income & asset breakdowns",
                "Money psychology & anti-impulse frameworks",
                "Step-by-step weekly financial checklists",
            ]
            keywords = ["personal finance habits", "wealth building systems", "solopreneur cashflow"]
        elif any(k in combined_signal for k in ("fit", "health", "gym", "mind", "motivat", "disciplin", "habit")):
            detected_niche = "High-Performance Mindset, Discipline & Daily Habits"
            pillars = [
                "Neuroscience-backed focus & dopamine reset habits",
                "Anti-procrastination execution frameworks",
                "Morning & evening routines of high performers",
                "Mental resilience checklists",
            ]
            keywords = ["high performance habits", "deep work discipline", "focus protocols"]
        elif any(k in combined_signal for k in ("design", "brand", "edit", "creat", "agency", "market", "social")):
            detected_niche = "Creator Growth, Content Systems & Digital Branding"
            pillars = [
                "Viral hook structures & 3-second retention rules",
                "High-save carousel & visual storytelling frameworks",
                "Monetizing small high-intent audiences",
                "Automated content production workflows",
            ]
            keywords = ["instagram growth systems", "creator economy tools", "content automation"]
        else:
            detected_niche = niche_hint or "AI Systems, Tech Leverage & Digital Productivity"
            pillars = [
                "AI workflow automation that saves 5+ hours/week",
                "Underrated tools, prompts, and systems breakdown",
                "Mental models for solo leverage and compounding",
                "Anti-hustle discipline and deep-work architecture",
            ]
            keywords = ["AI workflow automation", "solopreneur productivity systems", "AI agents"]

        avg_likes = 0.0
        avg_comments = 0.0
        reel_count = 0
        carousel_count = 0
        if recent_media:
            likes = [int(m.get("like_count") or 0) for m in recent_media]
            comments = [int(m.get("comments_count") or 0) for m in recent_media]
            avg_likes = round(sum(likes) / len(recent_media), 1)
            avg_comments = round(sum(comments) / len(recent_media), 1)
            for m in recent_media:
                mtype = str(m.get("media_type") or "").upper()
                if mtype in ("VIDEO", "REELS"):
                    reel_count += 1
                elif mtype == "CAROUSEL_ALBUM":
                    carousel_count += 1

        # Compute Audit Score & Diagnosis
        score = 72.0
        if biography:
            score += 8.0
        if followers > 100:
            score += 6.0
        if recent_media:
            score += 8.0
        score = min(round(score, 1), 96.0)

        bio_diagnosis = (
            f"Profile {handle} analyzed for '{detected_niche}'. "
            + (
                f"Detected Bio: \"{biography[:110]}\". "
                if biography
                else "No public bio text provided — add a 3-part bio: (1) Who you help, (2) Concrete outcome, (3) 'Save our daily systems' CTA."
            )
        )

        gaps = [
            "Reels Velocity: Post 1 short motion/photo Reel daily (18-35s) with burned-in pop captions to trigger non-follower Explore reach.",
            "Save-to-Reach Ratio: Pair every Reel with a 6-slide 4:5 Infographic Carousel and Single-Pic Cheatsheet so visitors bookmark your profile.",
            "A/B Hook Discipline: Lead captions with a concrete number or loss-aversion hook in the first 90 characters before the '...more' fold.",
            "Peak Window Consistency: Publish inside 09:00-11:00, 14:00-16:00, and 19:00-22:00 (Asia/Karachi) when audience scroll depth peaks.",
        ]
        if recent_media and reel_count == 0:
            gaps.insert(0, "Critical Gap: 0 Reels found in recent posts — Reels drive ~65% of new account discovery.")

        format_strategy = {
            "reel": "40% of mix — Cinematic photo pan/zoom + orbital/chart animation + voiceover + Lo-Fi BGM for cold reach.",
            "slide_video": "20% of mix — Animated diagram/step motion video for high completion & replay signals.",
            "carousel": "20% of mix — 6-slide 4:5 photo + SVG infographic deck to maximize Saves per Reach.",
            "single_pic": "10% of mix — 1-page Cheatsheet card for fast Story shares.",
            "text_post": "5% of mix — Operator Field Note + micro-essay caption to spark comment threads.",
            "story": "5% of mix — 3-frame Poll/Slider sequence to warm up existing followers before feed posts.",
        }

        top_ideas = [
            f"3 mistakes slowing down your {detected_niche.split(',')[0]} (and the 10-minute fix)",
            f"The 1-page {detected_niche.split(',')[0]} checklist for {handle} followers",
            f"Why 90% of people overcomplicate {pillars[0].lower()}",
            f"Before vs After: standardizing {pillars[1].lower()} in 3 steps",
            f"5 underrated rules for {pillars[2].lower()} in 2026",
        ]

        return AccountAuditReport(
            handle=handle,
            display_name=display_name,
            biography=biography,
            followers_count=followers,
            media_count=media_count,
            detected_niche=detected_niche,
            audit_score=score,
            data_source=data_source,
            bio_diagnosis=bio_diagnosis,
            algorithm_gaps=gaps,
            format_strategy=format_strategy,
            recommended_pillars=pillars,
            recommended_keywords=keywords,
            top_post_ideas=top_ideas,
            recent_posts_analyzed=len(recent_media),
            avg_likes=avg_likes,
            avg_comments=avg_comments,
        )

    def _update_env_credentials(
        self, ig_user_id: str, ig_access_token: str, gemini_api_key: str = ""
    ) -> None:
        env_path = self.settings.project_root / ".env"
        lines: list[str] = []
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").splitlines()
        elif (self.settings.project_root / ".env.example").exists():
            lines = (self.settings.project_root / ".env.example").read_text(encoding="utf-8").splitlines()

        updates: dict[str, str] = {"SMAI_LLM_PROVIDER": "gemini"}
        if ig_user_id:
            updates["IG_USER_ID"] = ig_user_id
        if ig_access_token:
            updates["IG_ACCESS_TOKEN"] = ig_access_token
        if gemini_api_key:
            updates["GEMINI_API_KEY"] = gemini_api_key

        seen: set[str] = set()
        new_lines: list[str] = []
        for line in lines:
            replaced = False
            for k, v in updates.items():
                if line.startswith(f"{k}="):
                    new_lines.append(f"{k}={v}")
                    seen.add(k)
                    replaced = True
                    break
            if not replaced:
                new_lines.append(line)
        for k, v in updates.items():
            if k not in seen:
                new_lines.append(f"{k}={v}")
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
