"""Smart window scheduler with randomized posting times and recurring daily draft cadence."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from smai.config import Settings, load_brand
from smai.db.database import Database
from smai.pipeline import SmaiPipeline
from smai.publishing import InstagramGraphClient


def pick_next_window_slot(
    windows: list[list[int]],
    timezone: str = "Asia/Karachi",
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> datetime:
    """Pick a jittered future timestamp within the configured posting hour windows."""
    tz = ZoneInfo(timezone)
    current = now.astimezone(tz) if now else datetime.now(tz)
    chooser = rng or random.Random()

    valid_windows = [w for w in windows if len(w) == 2 and 0 <= w[0] < w[1] <= 24]
    if not valid_windows:
        valid_windows = [[9, 11], [14, 16], [19, 22]]

    # Look for a remaining window today first, otherwise schedule tomorrow
    for day_offset in (0, 1):
        target_date = current.date() + timedelta(days=day_offset)
        candidates: list[tuple[int, int]] = []
        for start_hr, end_hr in valid_windows:
            earliest_min = start_hr * 60
            latest_min = end_hr * 60 - 1
            if day_offset == 0:
                now_min = current.hour * 60 + current.minute + 5
                earliest_min = max(earliest_min, now_min)
            if earliest_min <= latest_min:
                candidates.append((earliest_min, latest_min))
        if candidates:
            earliest, latest = chooser.choice(candidates)
            minute = chooser.randint(earliest, latest)
            return datetime.combine(
                target_date, time(hour=minute // 60, minute=minute % 60), tzinfo=tz
            )

    return current + timedelta(hours=2)


class SchedulerWorker:
    """Polls due schedules, runs daily research-to-draft cadence, and fetches IG insights."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.tz = ZoneInfo(settings.smai_timezone)
        self.scheduler = BackgroundScheduler(timezone=self.tz)

    def start(self) -> None:
        if not self.scheduler.running:
            brand = load_brand(self.settings.smai_default_brand, self.settings)
            draft_hour = max(0, min(23, int(brand.regular_cadence.daily_draft_hour)))
            self.scheduler.add_job(
                self.process_due_schedules,
                trigger=IntervalTrigger(seconds=30, timezone=self.tz),
                id="smai_due_publisher",
                replace_existing=True,
            )
            self.scheduler.add_job(
                self.run_daily_draft_cadence,
                trigger=CronTrigger(hour=draft_hour, minute=15, timezone=self.tz),
                id="smai_daily_draft_cadence",
                replace_existing=True,
            )
            self.scheduler.add_job(
                self.poll_due_insights,
                trigger=IntervalTrigger(hours=6, timezone=self.tz),
                id="smai_insights_poller",
                replace_existing=True,
            )
            self.scheduler.start()

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def run_daily_draft_cadence(self, dry_run_reel: bool = False) -> int:
        """Idempotently research trends and create today's full-format draft mix."""
        pipeline = SmaiPipeline(self.settings, self.db)
        _, brand_id = pipeline.ensure_brand()
        if self.db.count_drafts_created_today(brand_id) > 0:
            return 0
        results = asyncio.run(pipeline.run_full_draft_cycle(dry_run_reel=dry_run_reel))
        return len(results)

    def poll_due_insights(self) -> int:
        """Fetch live IG Graph API insights for recently published posts when configured."""
        if self.settings.smai_publish_mode.lower() != "graph_api":
            return 0
        client = InstagramGraphClient(self.settings)
        pipeline = SmaiPipeline(self.settings, self.db)
        logs = self.db.list_publish_logs(limit=10)
        captured = 0
        for row in logs:
            media_id = row.get("ig_media_id")
            if not media_id:
                continue
            try:
                snap = asyncio.run(client.fetch_insights(media_id, horizon_hours=24))
                pipeline.analyst.record_snapshot(int(row["id"]), snap)
                captured += 1
            except Exception:
                continue
        return captured

    def process_due_schedules(self) -> int:
        now = datetime.now(self.tz)
        queued = self.db.list_schedules(status="queued", limit=20)
        processed = 0
        pipeline = SmaiPipeline(self.settings, self.db)

        for row in queued:
            scheduled_dt = datetime.fromisoformat(row["scheduled_for"])
            if scheduled_dt.tzinfo is None:
                scheduled_dt = scheduled_dt.replace(tzinfo=self.tz)
            if scheduled_dt > now:
                continue

            schedule_id = int(row["id"])
            draft_id = int(row["draft_id"])
            self.db.update_schedule_status(schedule_id, "running")
            try:
                asyncio.run(pipeline.publish_draft(draft_id))
                self.db.update_schedule_status(schedule_id, "completed")
                processed += 1
            except Exception as exc:
                attempts = int(row["attempts"]) + 1
                next_status = "failed" if attempts >= 3 else "queued"
                self.db.update_schedule_status(
                    schedule_id,
                    next_status,
                    last_error=str(exc),
                    increment_attempts=True,
                )
        return processed
