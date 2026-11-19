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
from smai.db.models import (
    ApprovalRequiredError,
    DuplicatePublishError,
    InvalidStateTransitionError,
    normalize_approval_status,
)
from smai.pipeline import SmaiPipeline
from smai.publishing import InstagramGraphClient, MetaAPIError, redact_secret


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
        """Fetch live IG Graph API v27.0 account & 24h/72h media insights when credentials exist."""
        if not self.settings.has_meta_credentials():
            return 0
        client = InstagramGraphClient(self.settings)
        pipeline = SmaiPipeline(self.settings, self.db, graph_client=client)
        now = datetime.now(self.tz)
        captured = 0

        try:
            acct = asyncio.run(client.fetch_account_insights_safe(window_days=1))
            self.db.insert_account_insight(acct)
        except Exception:
            pass

        for horizon in (24, 72):
            targets = self.db.list_due_insight_targets(horizon_hours=horizon, limit=20)
            for row in targets:
                media_id = str(row.get("ig_media_id") or "").strip()
                if not media_id:
                    continue
                try:
                    pub_dt = datetime.fromisoformat(str(row["published_at"]))
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=self.tz)
                    if (now - pub_dt) < timedelta(hours=horizon):
                        continue
                except Exception:
                    pass

                fmt = str(row.get("draft_format") or "").lower()
                product_type = (
                    "REELS"
                    if fmt in ("reel", "slide_video")
                    else ("STORY" if fmt == "story" else "FEED")
                )
                try:
                    snap = asyncio.run(
                        client.fetch_media_insights_safe(
                            media_id,
                            media_product_type=product_type,
                            horizon_hours=horizon,
                        )
                    )
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
        mode = self.settings.smai_publish_mode.lower().strip()

        for row in queued:
            scheduled_dt = datetime.fromisoformat(row["scheduled_for"])
            if scheduled_dt.tzinfo is None:
                scheduled_dt = scheduled_dt.replace(tzinfo=self.tz)
            if scheduled_dt > now:
                continue

            schedule_id = int(row["id"])
            draft_id = int(row["draft_id"])
            draft = self.db.get_draft(draft_id)
            if not draft:
                self.db.update_schedule_status(
                    schedule_id,
                    "failed",
                    last_error=f"Draft {draft_id} not found.",
                    increment_attempts=True,
                )
                continue

            cur_status = normalize_approval_status(str(draft["approval_status"]))
            if cur_status != "scheduled":
                self.db.update_schedule_status(
                    schedule_id,
                    "failed",
                    last_error=(
                        f"Blocked execution: draft {draft_id} status is '{cur_status}' "
                        "(must be 'scheduled' after explicit approval)."
                    ),
                    increment_attempts=True,
                )
                continue

            self.db.update_schedule_status(schedule_id, "running")
            try:
                if mode == "export":
                    asyncio.run(pipeline.export_draft(draft_id))
                    self.db.update_schedule_status(schedule_id, "completed")
                elif mode == "browser":
                    asyncio.run(pipeline.stage_in_browser(draft_id))
                    self.db.update_schedule_status(schedule_id, "staged")
                else:
                    asyncio.run(pipeline.publish_draft(draft_id, mode=mode))
                    self.db.update_schedule_status(schedule_id, "completed")
                processed += 1
            except (ApprovalRequiredError, DuplicatePublishError, InvalidStateTransitionError) as exc:
                self.db.update_schedule_status(
                    schedule_id,
                    "failed",
                    last_error=str(exc),
                    increment_attempts=True,
                )
            except MetaAPIError as exc:
                attempts = int(row["attempts"]) + 1
                next_status = "queued" if (exc.is_transient and attempts < 3) else "failed"
                diag = redact_secret(str(exc), self.settings.ig_access_token)
                self.db.update_schedule_status(
                    schedule_id,
                    next_status,
                    last_error=diag,
                    increment_attempts=True,
                )
            except Exception as exc:
                attempts = int(row["attempts"]) + 1
                next_status = "failed" if attempts >= 3 else "queued"
                diag = redact_secret(str(exc), self.settings.ig_access_token)
                self.db.update_schedule_status(
                    schedule_id,
                    next_status,
                    last_error=diag,
                    increment_attempts=True,
                )
        return processed
