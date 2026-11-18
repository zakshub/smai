"""Trend & Topic Scout Agent: discovers, scores, and deduplicates faceless IG topics."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

import httpx
from pydantic import BaseModel

from smai.agents.algorithm import IGAlgorithmStrategist
from smai.agents.base import LLMClient
from smai.config import BrandBrain
from smai.db.database import Database
from smai.db.models import TopicCandidate


class ScoutBatch(BaseModel):
    topics: list[TopicCandidate]


def _normalize_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", text.lower()))


def is_duplicate_topic(candidate_title: str, existing_titles: list[str], threshold: float = 0.6) -> bool:
    """Jaccard token overlap check to avoid repeating near-identical topics."""
    cand_tokens = _normalize_tokens(candidate_title)
    if not cand_tokens:
        return False
    for existing in existing_titles:
        ext_tokens = _normalize_tokens(existing)
        if not ext_tokens:
            continue
        union = cand_tokens | ext_tokens
        overlap = len(cand_tokens & ext_tokens) / len(union)
        if overlap >= threshold:
            return True
    return False


async def fetch_rss_headlines(urls: list[str], limit_per_feed: int = 6) -> list[dict[str, str]]:
    """Fetch recent headlines from configured RSS/Atom feeds without external parsers."""
    items: list[dict[str, str]] = []
    if not urls:
        return items
    timeout = httpx.Timeout(3.0, connect=2.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for url in urls:
            try:
                res = await client.get(url)
                res.raise_for_status()
                root = ET.fromstring(res.text)
                for node in root.findall(".//item")[:limit_per_feed]:
                    title = (node.findtext("title") or "").strip()
                    link = (node.findtext("link") or "").strip()
                    if title:
                        items.append({"title": title, "url": link, "source": "rss"})
            except Exception:
                continue
    return items


async def fetch_keyword_trends(keywords: list[str], limit_per_kw: int = 3) -> list[dict[str, str]]:
    """Query Google News RSS for niche keyword trends."""
    if not keywords:
        return []
    urls = [
        f"https://news.google.com/rss/search?q={quote_plus(kw)}&hl=en-US&gl=US&ceid=US:en"
        for kw in keywords[:4]
    ]
    return await fetch_rss_headlines(urls, limit_per_feed=limit_per_kw)


async def fetch_reddit_trends(subreddits: list[str], limit_per_sub: int = 4) -> list[dict[str, str]]:
    """Fetch hot discussions from niche subreddits as pain-point signals."""
    items: list[dict[str, str]] = []
    if not subreddits:
        return items
    timeout = httpx.Timeout(3.0, connect=2.0)
    headers = {"User-Agent": "SMAI-Scout/0.1"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        for sub in subreddits[:4]:
            try:
                res = await client.get(
                    f"https://www.reddit.com/r/{sub.strip()}/hot.json?limit={limit_per_sub}"
                )
                res.raise_for_status()
                children = res.json().get("data", {}).get("children", [])
                for child in children:
                    data = child.get("data", {})
                    if data.get("stickied"):
                        continue
                    title = str(data.get("title", "")).strip()
                    permalink = str(data.get("permalink", "")).strip()
                    if title:
                        items.append(
                            {
                                "title": title,
                                "url": f"https://reddit.com{permalink}" if permalink else "",
                                "source": f"r/{sub}",
                            }
                        )
            except Exception:
                continue
    return items


class ScoutAgent:
    """Discovers and scores high-save/high-share Instagram topic candidates."""

    def __init__(self, llm: LLMClient, db: Database) -> None:
        self.llm = llm
        self.db = db
        self.strategist = IGAlgorithmStrategist()

    async def discover(
        self,
        brand: BrandBrain,
        brand_id: int,
        limit: int = 5,
        insights_summary: str = "",
        enable_network: bool = True,
    ) -> list[TopicCandidate]:
        existing_titles = self.db.list_topic_titles(brand_id)
        headlines: list[dict[str, str]] = []
        if enable_network:
            headlines.extend(await fetch_rss_headlines(brand.scout_sources.rss))
            headlines.extend(await fetch_keyword_trends(brand.scout_sources.keywords))
            headlines.extend(await fetch_reddit_trends(brand.scout_sources.subreddits))
        algo_guide = self.strategist.playbook_summary()

        system_prompt = (
            "You are an Instagram growth strategist for faceless pages who deeply understands the IG algorithm. "
            "Return strictly JSON matching {\"topics\": [{\"title\": str, \"angle\": str, "
            "\"pillar\": str, \"viral_score\": float, \"source_url\": str, \"rationale\": str, "
            "\"suggested_format\": \"carousel\"|\"reel\"|\"single_pic\"|\"text_post\"|\"story\", "
            "\"target_audience\": str, \"recommended_time\": str, \"algorithm_rationale\": str}]}. "
            "Prioritize concrete, save-worthy and share-worthy ideas."
        )
        user_prompt = (
            f"Brand: {brand.name} ({brand.niche})\n"
            f"Audience: {brand.audience}\n"
            f"Audience Segments: {[s.model_dump() for s in brand.audience_segments]}\n"
            f"Pillars: {brand.pillars}\n"
            f"Banned phrases: {brand.banned_phrases}\n"
            f"{algo_guide}\n"
            f"Recent RSS Signals: {headlines[:12]}\n"
            f"Seed Ideas: {brand.scout_sources.seed_topics}\n"
            f"Past Winners Summary: {insights_summary or 'None yet'}\n"
            f"Already Covered (avoid duplicates): {existing_titles[:20]}\n"
            f"Produce {limit} fresh topic candidates scored 1.0-10.0."
        )

        def _fallback() -> ScoutBatch:
            return self._local_fallback(brand, headlines, existing_titles, limit)

        batch = await self.llm.complete_json(
            agent_name="scout",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=ScoutBatch,
            fallback_factory=_fallback,
        )

        accepted: list[TopicCandidate] = []
        seen_now = list(existing_titles)
        for idx, topic in enumerate(batch.topics):
            if is_duplicate_topic(topic.title, seen_now):
                continue
            if not topic.target_audience or not topic.recommended_time or not topic.algorithm_rationale:
                rec = self.strategist.recommend(
                    brand, topic.title, index=idx, preferred_format=topic.suggested_format
                )
                topic.suggested_format = topic.suggested_format or rec.suggested_format
                topic.target_audience = topic.target_audience or rec.target_audience
                topic.recommended_time = topic.recommended_time or rec.recommended_time
                topic.algorithm_rationale = topic.algorithm_rationale or rec.algorithm_rationale

            self.db.insert_topic(
                brand_id=brand_id,
                title=topic.title,
                angle=topic.angle,
                pillar=topic.pillar,
                source_url=topic.source_url,
                viral_score=topic.viral_score,
                rationale=topic.rationale,
                suggested_format=topic.suggested_format,
                target_audience=topic.target_audience,
                recommended_time=topic.recommended_time,
                algorithm_rationale=topic.algorithm_rationale,
            )
            seen_now.append(topic.title)
            accepted.append(topic)
            if len(accepted) >= limit:
                break
        return accepted

    def _local_fallback(
        self,
        brand: BrandBrain,
        headlines: list[dict[str, str]],
        existing_titles: list[str],
        limit: int,
    ) -> ScoutBatch:
        candidates: list[TopicCandidate] = []
        pillars = brand.pillars or [brand.niche]

        for idx, item in enumerate(headlines):
            title = f"How builders can apply: {item['title'][:72]}"
            if not is_duplicate_topic(title, existing_titles):
                rec = self.strategist.recommend(brand, title, index=idx)
                candidates.append(
                    TopicCandidate(
                        title=title,
                        angle="Actionable 3-step breakdown for solo operators",
                        pillar=pillars[idx % len(pillars)],
                        viral_score=8.3,
                        source_url=item.get("url", ""),
                        rationale="Derived from live RSS tech/creator trend signal.",
                        suggested_format=rec.suggested_format,
                        target_audience=rec.target_audience,
                        recommended_time=rec.recommended_time,
                        algorithm_rationale=rec.algorithm_rationale,
                    )
                )

        for idx, seed in enumerate(brand.scout_sources.seed_topics):
            if not is_duplicate_topic(seed, existing_titles):
                rec = self.strategist.recommend(brand, seed, index=idx)
                candidates.append(
                    TopicCandidate(
                        title=seed,
                        angle="Contrarian mistake-to-system breakdown tailored to segment pain points",
                        pillar=pillars[idx % len(pillars)],
                        viral_score=round(8.8 - idx * 0.15, 2),
                        source_url="",
                        rationale="Evergreen BrandBrain pillar aligned with high saves & shares.",
                        suggested_format=rec.suggested_format,
                        target_audience=rec.target_audience,
                        recommended_time=rec.recommended_time,
                        algorithm_rationale=rec.algorithm_rationale,
                    )
                )

        if not candidates:
            title = f"5-minute {brand.niche.split(',')[0]} audit for weekly leverage"
            rec = self.strategist.recommend(brand, title, index=0)
            candidates.append(
                TopicCandidate(
                    title=title,
                    angle="Step-by-step checklist people save and revisit on Monday",
                    pillar=pillars[0],
                    viral_score=8.0,
                    rationale="Fallback evergreen checklist.",
                    suggested_format=rec.suggested_format,
                    target_audience=rec.target_audience,
                    recommended_time=rec.recommended_time,
                    algorithm_rationale=rec.algorithm_rationale,
                )
            )

        return ScoutBatch(topics=candidates[:limit])
