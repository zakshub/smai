"""Unit tests for BrandBrain, Database, Scout, Writer, Critic, and Analyst agents."""

from __future__ import annotations

import asyncio

from smai.agents import (
    AnalystAgent,
    CriticAgent,
    InstagramAccountAnalyzer,
    LLMClient,
    ScoutAgent,
    WriterAgent,
)
from smai.agents.account_analyzer import normalize_handle
from smai.agents.scout import is_duplicate_topic
from smai.config import Settings, load_brand
from smai.db.database import get_database
from smai.db.models import InsightSnapshot, TopicCandidate


def _temp_settings(tmp_path) -> Settings:
    return Settings(
        smai_data_dir=tmp_path / "data",
        smai_export_dir=tmp_path / "exports",
        smai_llm_provider="mock",
        smai_tts_provider="silent",
        smai_enable_network_scout=False,
        smai_prefer_playwright=False,
    )


def test_brand_load_and_db_sync(tmp_path):
    settings = _temp_settings(tmp_path)
    brand = load_brand("default", settings)
    db = get_database(settings)
    brand_id = db.upsert_brand(brand)
    row = db.get_brand_row("default")
    assert brand_id > 0
    assert row is not None
    assert row["name"] == brand.name


def test_topic_deduplication():
    existing = ["3-step AI morning brief that replaces 45 minutes of inbox triage"]
    assert is_duplicate_topic(
        "3 step AI morning brief replacing 45 minutes inbox triage", existing
    )
    assert not is_duplicate_topic("Why deep work beats multitasking for designers", existing)


def test_scout_writer_critic_analyst_flow(tmp_path):
    settings = _temp_settings(tmp_path)
    brand = load_brand("default", settings)
    db = get_database(settings)
    brand_id = db.upsert_brand(brand)
    llm = LLMClient(settings, db=db)

    scout = ScoutAgent(llm, db)
    topics = asyncio.run(scout.discover(brand, brand_id, limit=3, enable_network=False))
    assert len(topics) >= 1
    assert len(db.list_topics(brand_id, status="new")) >= 1

    writer = WriterAgent(llm)
    topic = TopicCandidate(
        title="Stop collecting prompts: build checklists instead",
        angle="Practical operator system",
        pillar="AI workflow automation",
    )
    carousel_pkg = asyncio.run(writer.write(brand, topic, fmt="carousel"))
    assert carousel_pkg.format == "carousel"
    assert len(carousel_pkg.slides) >= 5
    assert len(carousel_pkg.ab_hooks) >= 2

    critic = CriticAgent()
    qa = critic.evaluate(brand, carousel_pkg)
    assert qa.status == "pass"
    assert qa.score == 100.0

    # Verify banned phrase detection triggers revise
    carousel_pkg.caption = "This is a game changer to unlock your full potential!"
    bad_qa = critic.evaluate(brand, carousel_pkg)
    assert bad_qa.status == "revise"
    assert any("banned" in note.lower() for note in bad_qa.revision_instructions)

    # Verify analyst scoring favors saves & shares
    draft_id = db.insert_draft(
        brand_id=brand_id,
        topic_id=None,
        fmt="carousel",
        title=carousel_pkg.title,
        script_json=carousel_pkg.model_dump_json(),
        caption=carousel_pkg.caption,
        hashtags="#aitools",
        ab_hooks_json="[]",
    )
    pub_id = db.record_publish(draft_id, mode="export", export_path=str(tmp_path / "exp"))
    analyst = AnalystAgent(db)
    snap = InsightSnapshot(reach=1000, plays=1500, likes=80, comments=10, saves=50, shares=20)
    analyst.record_snapshot(pub_id, snap)
    summary = analyst.build_feedback_summary()
    assert "Stop collecting prompts" in summary
    assert snap.engagement_score > 40.0


def test_instagram_account_analyzer(tmp_path):
    assert normalize_handle("https://www.instagram.com/zakshub.ai/") == "zakshub.ai"
    assert normalize_handle("@quietleverage.ai") == "quietleverage.ai"

    settings = _temp_settings(tmp_path)
    db = get_database(settings)
    analyzer = InstagramAccountAnalyzer(settings, db)
    report = asyncio.run(
        analyzer.analyze(
            handle_or_url="https://instagram.com/zakshub.ai",
            bio_hint="Helping solo builders automate workflows with AI agents",
            niche_hint="AI Systems & Automation",
            enable_network=False,
        )
    )
    assert report.handle == "@zakshub.ai"
    assert report.audit_score >= 75.0
    assert len(report.algorithm_gaps) >= 3
    assert "reel" in report.format_strategy

    db.save_account_audit("default", report.model_dump_json())
    saved = db.get_latest_account_audit("default")
    assert saved is not None
    assert saved["handle"] == "@zakshub.ai"
