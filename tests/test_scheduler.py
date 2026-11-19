"""Tests for smart window scheduler and due draft processing."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from smai.config import Settings
from smai.db.database import get_database
from smai.pipeline import SmaiPipeline
from smai.scheduler.worker import SchedulerWorker, pick_next_window_slot


def test_pick_next_window_slot_respects_future_windows():
    tz = ZoneInfo("Asia/Karachi")
    morning_now = datetime(2026, 11, 17, 8, 0, tzinfo=tz)
    slot = pick_next_window_slot(
        windows=[[9, 11], [14, 16]],
        timezone="Asia/Karachi",
        now=morning_now,
        rng=random.Random(42),
    )
    assert slot > morning_now
    assert slot.hour in (9, 10, 14, 15)

    # Late night rolls into next day
    late_now = datetime(2026, 11, 17, 23, 30, tzinfo=tz)
    tomorrow_slot = pick_next_window_slot(
        windows=[[9, 11]],
        timezone="Asia/Karachi",
        now=late_now,
        rng=random.Random(42),
    )
    assert tomorrow_slot.date() == (late_now + timedelta(days=1)).date()
    assert tomorrow_slot.hour in (9, 10)


def test_scheduler_worker_processes_due_draft(tmp_path):
    settings = Settings(
        smai_data_dir=tmp_path / "data",
        smai_export_dir=tmp_path / "exports",
        smai_llm_provider="mock",
        smai_tts_provider="silent",
        smai_publish_mode="export",
        smai_enable_network_scout=False,
        smai_prefer_playwright=False,
    )
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)
    res = asyncio.run(
        pipeline.generate_draft(
            fmt="carousel",
            custom_topic="Scheduled system breakdown",
            render_media=True,
            prefer_playwright=False,
        )
    )
    draft_id = int(res["draft"]["id"])
    past_due = (datetime.now(ZoneInfo("Asia/Karachi")) - timedelta(minutes=2)).isoformat()
    sched_id = db.create_schedule(draft_id, past_due)

    worker = SchedulerWorker(settings, db)
    processed = worker.process_due_schedules()
    assert processed == 1

    schedules = db.list_schedules()
    match = next(s for s in schedules if s["id"] == sched_id)
    assert match["status"] == "completed"
    assert db.get_draft(draft_id)["approval_status"] == "published"


def test_daily_draft_cadence_is_idempotent(tmp_path):
    settings = Settings(
        smai_data_dir=tmp_path / "data",
        smai_export_dir=tmp_path / "exports",
        smai_llm_provider="mock",
        smai_tts_provider="silent",
        smai_publish_mode="export",
        smai_enable_network_scout=False,
        smai_prefer_playwright=False,
    )
    db = get_database(settings)
    worker = SchedulerWorker(settings, db)

    first_count = worker.run_daily_draft_cadence(dry_run_reel=True)
    assert first_count == 6

    # Calling again the same day must not create duplicate daily batches
    second_count = worker.run_daily_draft_cadence(dry_run_reel=True)
    assert second_count == 0


def test_scheduler_does_not_retry_duplicate_publish(tmp_path):
    settings = Settings(
        smai_data_dir=tmp_path / "data",
        smai_export_dir=tmp_path / "exports",
        smai_llm_provider="mock",
        smai_tts_provider="silent",
        smai_publish_mode="export",
        smai_enable_network_scout=False,
        smai_prefer_playwright=False,
    )
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)
    res = asyncio.run(
        pipeline.generate_draft(
            fmt="single_pic",
            custom_topic="Already published draft",
            render_media=True,
            prefer_playwright=False,
        )
    )
    draft_id = int(res["draft"]["id"])
    asyncio.run(pipeline.publish_draft(draft_id, mode="export"))

    past_due = (datetime.now(ZoneInfo("Asia/Karachi")) - timedelta(minutes=1)).isoformat()
    sched_id = db.create_schedule(draft_id, past_due)

    worker = SchedulerWorker(settings, db)
    processed = worker.process_due_schedules()
    assert processed == 0

    match = next(s for s in db.list_schedules() if s["id"] == sched_id)
    assert match["status"] == "failed"
    assert match["attempts"] == 1
