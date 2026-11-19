"""SQLite persistence layer and schema management for SMAI."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from smai.config import BrandBrain, Settings
from smai.db.models import (
    AccountInsightResult,
    ApprovalRequiredError,
    DuplicatePublishError,
    InsightSnapshot,
    InvalidStateTransitionError,
    can_transition_status,
    normalize_approval_status,
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS brands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    niche TEXT NOT NULL,
    config_json TEXT NOT NULL,
    ig_user_id TEXT DEFAULT '',
    timezone TEXT DEFAULT 'Asia/Karachi',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    angle TEXT NOT NULL,
    pillar TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    viral_score REAL DEFAULT 7.0,
    rationale TEXT DEFAULT '',
    suggested_format TEXT DEFAULT 'carousel',
    target_audience TEXT DEFAULT '',
    recommended_time TEXT DEFAULT '',
    algorithm_rationale TEXT DEFAULT '',
    status TEXT DEFAULT 'new',
    created_at TEXT NOT NULL,
    FOREIGN KEY (brand_id) REFERENCES brands(id)
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    topic_id INTEGER,
    format TEXT NOT NULL,
    title TEXT NOT NULL,
    script_json TEXT NOT NULL,
    caption TEXT NOT NULL,
    hashtags TEXT DEFAULT '',
    target_audience TEXT DEFAULT '',
    recommended_time TEXT DEFAULT '',
    algorithm_rationale TEXT DEFAULT '',
    selected_hook_idx INTEGER DEFAULT 0,
    ab_hooks_json TEXT DEFAULT '[]',
    qa_status TEXT DEFAULT 'pending',
    qa_score REAL DEFAULT 0.0,
    qa_notes TEXT DEFAULT '[]',
    approval_status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (brand_id) REFERENCES brands(id),
    FOREIGN KEY (topic_id) REFERENCES topics(id)
);

CREATE TABLE IF NOT EXISTS media_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    asset_type TEXT NOT NULL,
    slide_index INTEGER DEFAULT 0,
    local_path TEXT NOT NULL,
    public_url TEXT DEFAULT '',
    width INTEGER DEFAULT 0,
    height INTEGER DEFAULT 0,
    duration_sec REAL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (draft_id) REFERENCES drafts(id)
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    scheduled_for TEXT NOT NULL,
    status TEXT DEFAULT 'queued',
    attempts INTEGER DEFAULT 0,
    last_error TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (draft_id) REFERENCES drafts(id)
);

CREATE TABLE IF NOT EXISTS publish_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    mode TEXT NOT NULL,
    outcome TEXT DEFAULT 'published',
    ig_container_id TEXT DEFAULT '',
    ig_media_id TEXT DEFAULT '',
    permalink TEXT DEFAULT '',
    export_path TEXT DEFAULT '',
    published_at TEXT NOT NULL,
    FOREIGN KEY (draft_id) REFERENCES drafts(id)
);

CREATE TABLE IF NOT EXISTS insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publish_log_id INTEGER NOT NULL,
    horizon_hours INTEGER NOT NULL,
    reach INTEGER,
    views INTEGER,
    plays INTEGER,
    likes INTEGER,
    comments INTEGER,
    saves INTEGER,
    shares INTEGER,
    profile_visits INTEGER,
    follows INTEGER,
    avg_watch_time_ms INTEGER,
    engagement_score REAL,
    source TEXT DEFAULT 'manual',
    raw_metrics_json TEXT DEFAULT '{}',
    unsupported_metrics_json TEXT DEFAULT '[]',
    captured_at TEXT NOT NULL,
    FOREIGN KEY (publish_log_id) REFERENCES publish_logs(id)
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    draft_id INTEGER,
    model TEXT NOT NULL,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    status TEXT NOT NULL,
    error_message TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_slug TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

V2_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_slug TEXT NOT NULL,
    ig_user_id TEXT DEFAULT '',
    handle TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    biography TEXT DEFAULT '',
    followers_count INTEGER,
    follows_count INTEGER,
    media_count INTEGER,
    website TEXT DEFAULT '',
    source TEXT NOT NULL,
    raw_json TEXT DEFAULT '{}',
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_media_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER,
    handle TEXT NOT NULL,
    ig_media_id TEXT DEFAULT '',
    media_type TEXT DEFAULT '',
    media_product_type TEXT DEFAULT '',
    caption TEXT DEFAULT '',
    permalink TEXT DEFAULT '',
    like_count INTEGER,
    comments_count INTEGER,
    reach INTEGER,
    views INTEGER,
    saves INTEGER,
    shares INTEGER,
    media_timestamp TEXT DEFAULT '',
    source TEXT NOT NULL,
    raw_json TEXT DEFAULT '{}',
    captured_at TEXT NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES account_snapshots(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_account_media_handle_id
ON account_media_cache(handle, ig_media_id)
WHERE ig_media_id != '';

CREATE TABLE IF NOT EXISTS account_insight_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ig_user_id TEXT DEFAULT '',
    window_days INTEGER NOT NULL DEFAULT 1,
    reach INTEGER,
    views INTEGER,
    profile_views INTEGER,
    accounts_engaged INTEGER,
    total_interactions INTEGER,
    follower_delta INTEGER,
    raw_metrics_json TEXT DEFAULT '{}',
    unsupported_metrics_json TEXT DEFAULT '[]',
    metric_errors_json TEXT DEFAULT '{}',
    source TEXT NOT NULL,
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    variable_name TEXT NOT NULL,
    control_value TEXT NOT NULL,
    treatment_value TEXT NOT NULL,
    primary_metric TEXT DEFAULT 'engagement_score',
    min_sample_per_arm INTEGER DEFAULT 5,
    status TEXT DEFAULT 'active',
    winner TEXT DEFAULT '',
    confidence TEXT DEFAULT 'none',
    notes TEXT DEFAULT '',
    approved_by_operator INTEGER DEFAULT 0,
    approved_at TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL,
    draft_id INTEGER NOT NULL,
    arm TEXT NOT NULL,
    variant_value TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    FOREIGN KEY (experiment_id) REFERENCES experiments(id),
    FOREIGN KEY (draft_id) REFERENCES drafts(id),
    UNIQUE(experiment_id, draft_id)
);

CREATE TABLE IF NOT EXISTS experiment_learnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL,
    brand_id INTEGER NOT NULL,
    summary TEXT NOT NULL,
    recommended_rule TEXT NOT NULL,
    sample_size_control INTEGER DEFAULT 0,
    sample_size_treatment INTEGER DEFAULT 0,
    control_metric_avg REAL,
    treatment_metric_avg REAL,
    lift_pct REAL,
    confidence TEXT DEFAULT 'none',
    winner TEXT DEFAULT 'inconclusive',
    status TEXT DEFAULT 'pending_approval',
    approved_by_operator INTEGER DEFAULT 0,
    promoted_at TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (experiment_id) REFERENCES experiments(id)
);
"""


class Database:
    """Lightweight SQLite repository for SMAI state."""

    def __init__(self, db_path: Path, timezone: str = "Asia/Karachi") -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.tz = ZoneInfo(timezone)

    def now_iso(self) -> str:
        return datetime.now(self.tz).isoformat()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def init_schema(self) -> None:
        with self.connect() as con:
            con.executescript(SCHEMA_SQL)
            con.executescript(V2_SCHEMA_SQL)
            self._ensure_columns(
                con,
                "topics",
                {
                    "suggested_format": "TEXT DEFAULT 'carousel'",
                    "target_audience": "TEXT DEFAULT ''",
                    "recommended_time": "TEXT DEFAULT ''",
                    "algorithm_rationale": "TEXT DEFAULT ''",
                },
            )
            self._ensure_columns(
                con,
                "drafts",
                {
                    "target_audience": "TEXT DEFAULT ''",
                    "recommended_time": "TEXT DEFAULT ''",
                    "algorithm_rationale": "TEXT DEFAULT ''",
                    "last_publish_error": "TEXT DEFAULT ''",
                    "last_error_at": "TEXT DEFAULT ''",
                    "export_path": "TEXT DEFAULT ''",
                    "staged_at": "TEXT DEFAULT ''",
                    "published_at": "TEXT DEFAULT ''",
                },
            )
            self._ensure_columns(
                con,
                "publish_logs",
                {
                    "outcome": "TEXT DEFAULT 'published'",
                    "ig_container_id": "TEXT DEFAULT ''",
                },
            )
            self._ensure_columns(
                con,
                "insights",
                {
                    "views": "INTEGER",
                    "profile_visits": "INTEGER",
                    "follows": "INTEGER",
                    "avg_watch_time_ms": "INTEGER",
                    "source": "TEXT DEFAULT 'manual'",
                    "raw_metrics_json": "TEXT DEFAULT '{}'",
                    "unsupported_metrics_json": "TEXT DEFAULT '[]'",
                },
            )
            # Normalize legacy 'pending' drafts -> 'pending_approval' idempotently
            con.execute(
                "UPDATE drafts SET approval_status = 'pending_approval' WHERE approval_status = 'pending'"
            )
            # Mark historical local export rows accurately as outcome='exported'
            con.execute(
                "UPDATE publish_logs SET outcome = 'exported' WHERE mode = 'export' AND (outcome IS NULL OR outcome = 'published')"
            )
            # Deduplicate historical insights before adding unique index on (publish_log_id, horizon_hours)
            con.execute(
                """
                DELETE FROM insights
                WHERE id NOT IN (
                    SELECT MAX(id) FROM insights GROUP BY publish_log_id, horizon_hours
                )
                """
            )
            con.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_insights_log_horizon
                ON insights(publish_log_id, horizon_hours)
                """
            )

    @staticmethod
    def _ensure_columns(con: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, col_def in columns.items():
            if col not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")

    def upsert_brand(self, brand: BrandBrain, ig_user_id: str = "") -> int:
        now = self.now_iso()
        cfg_json = brand.model_dump_json()
        with self.connect() as con:
            existing = con.execute(
                "SELECT id FROM brands WHERE slug = ?", (brand.slug,)
            ).fetchone()
            if existing:
                con.execute(
                    """
                    UPDATE brands
                    SET name = ?, niche = ?, config_json = ?, ig_user_id = ?, timezone = ?
                    WHERE id = ?
                    """,
                    (
                        brand.name,
                        brand.niche,
                        cfg_json,
                        ig_user_id,
                        brand.timezone,
                        existing["id"],
                    ),
                )
                return int(existing["id"])
            cur = con.execute(
                """
                INSERT INTO brands (slug, name, niche, config_json, ig_user_id, timezone, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    brand.slug,
                    brand.name,
                    brand.niche,
                    cfg_json,
                    ig_user_id,
                    brand.timezone,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def get_brand_row(self, slug: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM brands WHERE slug = ?", (slug,)).fetchone()
            return dict(row) if row else None

    def list_topic_titles(self, brand_id: int) -> list[str]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT title FROM topics WHERE brand_id = ? ORDER BY id DESC",
                (brand_id,),
            ).fetchall()
            return [str(r["title"]) for r in rows]

    def insert_topic(
        self,
        brand_id: int,
        title: str,
        angle: str,
        pillar: str = "",
        source_url: str = "",
        viral_score: float = 7.0,
        rationale: str = "",
        suggested_format: str = "carousel",
        target_audience: str = "",
        recommended_time: str = "",
        algorithm_rationale: str = "",
    ) -> int:
        with self.connect() as con:
            cur = con.execute(
                """
                INSERT INTO topics (
                    brand_id, title, angle, pillar, source_url, viral_score, rationale,
                    suggested_format, target_audience, recommended_time, algorithm_rationale,
                    status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
                """,
                (
                    brand_id,
                    title,
                    angle,
                    pillar,
                    source_url,
                    viral_score,
                    rationale,
                    suggested_format,
                    target_audience,
                    recommended_time,
                    algorithm_rationale,
                    self.now_iso(),
                ),
            )
            return int(cur.lastrowid)

    def list_topics(
        self, brand_id: int, status: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM topics WHERE brand_id = ?"
        params: list[Any] = [brand_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY viral_score DESC, id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as con:
            rows = con.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def mark_topic_status(self, topic_id: int, status: str) -> None:
        with self.connect() as con:
            con.execute("UPDATE topics SET status = ? WHERE id = ?", (status, topic_id))

    def insert_draft(
        self,
        brand_id: int,
        topic_id: int | None,
        fmt: str,
        title: str,
        script_json: str,
        caption: str,
        hashtags: str,
        ab_hooks_json: str,
        target_audience: str = "",
        recommended_time: str = "",
        algorithm_rationale: str = "",
    ) -> int:
        now = self.now_iso()
        with self.connect() as con:
            cur = con.execute(
                """
                INSERT INTO drafts (
                    brand_id, topic_id, format, title, script_json, caption,
                    hashtags, target_audience, recommended_time, algorithm_rationale,
                    selected_hook_idx, ab_hooks_json, qa_status,
                    qa_score, qa_notes, approval_status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'pending', 0.0, '[]', 'draft', ?, ?)
                """,
                (
                    brand_id,
                    topic_id,
                    fmt,
                    title,
                    script_json,
                    caption,
                    hashtags,
                    target_audience,
                    recommended_time,
                    algorithm_rationale,
                    ab_hooks_json,
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def update_draft_qa(
        self, draft_id: int, qa_status: str, qa_score: float, qa_notes: list[str]
    ) -> None:
        now = self.now_iso()
        with self.connect() as con:
            con.execute(
                """
                UPDATE drafts
                SET qa_status = ?,
                    qa_score = ?,
                    qa_notes = ?,
                    approval_status = CASE
                        WHEN approval_status = 'draft' THEN 'pending_approval'
                        ELSE approval_status
                    END,
                    updated_at = ?
                WHERE id = ?
                """,
                (qa_status, qa_score, json.dumps(qa_notes), now, draft_id),
            )

    def update_draft_content(
        self,
        draft_id: int,
        caption: str | None = None,
        selected_hook_idx: int | None = None,
        script_json: str | None = None,
        approval_status: str | None = None,
    ) -> None:
        now = self.now_iso()
        with self.connect() as con:
            row = con.execute("SELECT approval_status FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            if not row:
                raise ValueError(f"Draft {draft_id} not found.")
            current_status = normalize_approval_status(str(row["approval_status"]))

            fields: list[str] = []
            values: list[Any] = []
            if caption is not None:
                fields.append("caption = ?")
                values.append(caption)
            if selected_hook_idx is not None:
                fields.append("selected_hook_idx = ?")
                values.append(selected_hook_idx)
            if script_json is not None:
                fields.append("script_json = ?")
                values.append(script_json)
            if approval_status is not None:
                target_status = normalize_approval_status(approval_status)
                if not can_transition_status(current_status, target_status):
                    raise InvalidStateTransitionError(
                        f"Cannot transition draft {draft_id} from '{current_status}' to '{target_status}'."
                    )
                fields.append("approval_status = ?")
                values.append(target_status)
                if target_status in ("approved", "pending_approval", "draft"):
                    fields.append("last_publish_error = ?")
                    values.append("")
                if target_status in ("rejected", "draft", "pending_approval"):
                    con.execute(
                        """
                        UPDATE schedules
                        SET status = 'cancelled',
                            last_error = 'Cancelled due to draft status update',
                            updated_at = ?
                        WHERE draft_id = ? AND status = 'queued'
                        """,
                        (now, draft_id),
                    )
            if not fields:
                return
            fields.append("updated_at = ?")
            values.append(now)
            values.append(draft_id)
            con.execute(
                f"UPDATE drafts SET {', '.join(fields)} WHERE id = ?",
                values,
            )

    def transition_draft_status(
        self,
        draft_id: int,
        target_status: str,
        *,
        error_message: str | None = None,
        export_path: str | None = None,
    ) -> dict[str, Any]:
        """Atomically validate and execute a draft lifecycle state transition."""
        now = self.now_iso()
        nxt = normalize_approval_status(target_status)
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            if not row:
                raise ValueError(f"Draft {draft_id} not found.")
            cur = normalize_approval_status(str(row["approval_status"]))
            if not can_transition_status(cur, nxt):
                raise InvalidStateTransitionError(
                    f"Cannot transition draft {draft_id} from '{cur}' to '{nxt}'."
                )

            updates = ["approval_status = ?", "updated_at = ?"]
            params: list[Any] = [nxt, now]
            if error_message is not None:
                updates.extend(["last_publish_error = ?", "last_error_at = ?"])
                params.extend([error_message, now if error_message else ""])
            elif nxt in ("approved", "pending_approval", "draft", "published"):
                updates.append("last_publish_error = ''")

            if export_path is not None:
                updates.append("export_path = ?")
                params.append(export_path)
            if nxt == "staged":
                updates.append("staged_at = ?")
                params.append(now)
            if nxt == "published":
                updates.append("published_at = ?")
                params.append(now)

            params.append(draft_id)
            con.execute(f"UPDATE drafts SET {', '.join(updates)} WHERE id = ?", params)

            if nxt in ("rejected", "draft", "pending_approval"):
                con.execute(
                    """
                    UPDATE schedules
                    SET status = 'cancelled',
                        last_error = 'Cancelled due to status transition',
                        updated_at = ?
                    WHERE draft_id = ? AND status = 'queued'
                    """,
                    (now, draft_id),
                )

            updated = con.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            con.commit()
            return dict(updated)
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def get_draft(self, draft_id: int) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            return dict(row) if row else None

    def count_drafts_created_today(self, brand_id: int) -> int:
        today_prefix = datetime.now(self.tz).date().isoformat()
        with self.connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS cnt FROM drafts WHERE brand_id = ? AND created_at LIKE ?",
                (brand_id, f"{today_prefix}%"),
            ).fetchone()
            return int(row["cnt"])

    def list_drafts(
        self, brand_id: int | None = None, approval_status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM drafts WHERE 1=1"
        params: list[Any] = []
        if brand_id is not None:
            query += " AND brand_id = ?"
            params.append(brand_id)
        if approval_status:
            query += " AND approval_status = ?"
            params.append(approval_status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as con:
            rows = con.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def replace_media_assets(
        self, draft_id: int, assets: list[dict[str, Any]]
    ) -> None:
        now = self.now_iso()
        with self.connect() as con:
            con.execute("DELETE FROM media_assets WHERE draft_id = ?", (draft_id,))
            for item in assets:
                con.execute(
                    """
                    INSERT INTO media_assets (
                        draft_id, asset_type, slide_index, local_path, public_url,
                        width, height, duration_sec, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        draft_id,
                        item["asset_type"],
                        item.get("slide_index", 0),
                        item["local_path"],
                        item.get("public_url", ""),
                        item.get("width", 0),
                        item.get("height", 0),
                        item.get("duration_sec", 0.0),
                        now,
                    ),
                )

    def list_media_assets(self, draft_id: int) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM media_assets WHERE draft_id = ? ORDER BY slide_index ASC, id ASC",
                (draft_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_asset_public_url(self, asset_id: int, public_url: str) -> None:
        with self.connect() as con:
            con.execute(
                "UPDATE media_assets SET public_url = ? WHERE id = ?",
                (public_url, asset_id),
            )

    def create_schedule(self, draft_id: int, scheduled_for: str) -> int:
        return self.schedule_draft_atomic(draft_id, scheduled_for)

    def schedule_draft_atomic(self, draft_id: int, scheduled_for: str) -> int:
        """
        Atomically verify draft is approved/exported, ensure no duplicate queued schedule exists,
        insert queued schedule, and set draft approval_status = 'scheduled'.
        """
        now = self.now_iso()
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT approval_status FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Draft {draft_id} not found.")
            cur_status = normalize_approval_status(str(row["approval_status"]))
            if cur_status in ("published", "publishing"):
                raise DuplicatePublishError(
                    f"Draft {draft_id} is already '{cur_status}' and cannot be scheduled."
                )
            if cur_status not in ("approved", "exported"):
                if cur_status in ("scheduled", "staged"):
                    raise InvalidStateTransitionError(
                        f"Draft {draft_id} is already '{cur_status}'."
                    )
                raise ApprovalRequiredError(
                    f"Draft {draft_id} has status '{cur_status}' and must be approved before scheduling."
                )
            existing = con.execute(
                "SELECT id FROM schedules WHERE draft_id = ? AND status = 'queued'",
                (draft_id,),
            ).fetchone()
            if existing:
                raise InvalidStateTransitionError(
                    f"Draft {draft_id} already has active queued schedule #{existing['id']}."
                )
            cur = con.execute(
                """
                INSERT INTO schedules (draft_id, scheduled_for, status, attempts, last_error, created_at, updated_at)
                VALUES (?, ?, 'queued', 0, '', ?, ?)
                """,
                (draft_id, scheduled_for, now, now),
            )
            con.execute(
                "UPDATE drafts SET approval_status = 'scheduled', last_publish_error = '', updated_at = ? WHERE id = ?",
                (now, draft_id),
            )
            con.commit()
            return int(cur.lastrowid)
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def cancel_schedule_atomic(self, schedule_id: int, reason: str = "Cancelled by operator") -> dict[str, Any]:
        """Cancel a queued schedule and restore its draft back to approved/exported."""
        now = self.now_iso()
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            con.execute("BEGIN IMMEDIATE")
            sched = con.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
            if not sched:
                raise ValueError(f"Schedule {schedule_id} not found.")
            if sched["status"] != "queued":
                raise InvalidStateTransitionError(
                    f"Schedule {schedule_id} has status '{sched['status']}' and cannot be cancelled."
                )
            con.execute(
                "UPDATE schedules SET status = 'cancelled', last_error = ?, updated_at = ? WHERE id = ?",
                (reason, now, schedule_id),
            )
            draft_id = int(sched["draft_id"])
            draft = con.execute("SELECT approval_status, export_path FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            if draft and normalize_approval_status(str(draft["approval_status"])) == "scheduled":
                restored = "exported" if (draft["export_path"] or "") else "approved"
                con.execute(
                    "UPDATE drafts SET approval_status = ?, updated_at = ? WHERE id = ?",
                    (restored, now, draft_id),
                )
            updated = con.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
            con.commit()
            return dict(updated)
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def list_schedules(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query = """
            SELECT s.*, d.title AS draft_title, d.format AS draft_format, d.approval_status AS draft_status
            FROM schedules s
            JOIN drafts d ON d.id = s.draft_id
        """
        params: list[Any] = []
        if status:
            query += " WHERE s.status = ?"
            params.append(status)
        query += " ORDER BY s.scheduled_for ASC LIMIT ?"
        params.append(limit)
        with self.connect() as con:
            rows = con.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def update_schedule_status(
        self, schedule_id: int, status: str, last_error: str = "", increment_attempts: bool = False
    ) -> None:
        now = self.now_iso()
        with self.connect() as con:
            if increment_attempts:
                con.execute(
                    """
                    UPDATE schedules
                    SET status = ?, last_error = ?, attempts = attempts + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, last_error, now, schedule_id),
                )
            else:
                con.execute(
                    """
                    UPDATE schedules
                    SET status = ?, last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, last_error, now, schedule_id),
                )

    def claim_publish_lock(self, draft_id: int, mode: str) -> str | None:
        """
        Atomically enforce approval and claim an in-flight publish lock for a draft so
        unapproved or duplicate publishing is blocked. Returns previous approval_status when
        acquired, returns None if already published / in progress, and raises
        ApprovalRequiredError / InvalidStateTransitionError when draft state does not allow publishing.
        """
        now = self.now_iso()
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT approval_status FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Draft {draft_id} not found.")
            prev_status = normalize_approval_status(str(row["approval_status"]))
            if prev_status in ("published", "publishing"):
                con.rollback()
                return None

            existing_log = con.execute(
                "SELECT id, mode, outcome, ig_media_id FROM publish_logs WHERE draft_id = ?",
                (draft_id,),
            ).fetchall()
            for log in existing_log:
                log_outcome = str(log["outcome"] or "published")
                if (
                    log_outcome == "published"
                    or log["ig_media_id"]
                    or log["mode"] in ("graph_api", "browser_share", "ig_browser_confirmed")
                ):
                    con.rollback()
                    return None

            if prev_status in ("draft", "pending_approval", "rejected", "failed"):
                con.rollback()
                raise ApprovalRequiredError(
                    f"Draft {draft_id} has status '{prev_status}' and must be approved before publishing."
                )
            if prev_status not in ("approved", "exported", "scheduled"):
                con.rollback()
                raise InvalidStateTransitionError(
                    f"Draft {draft_id} is in status '{prev_status}' and cannot start '{mode}' publish directly."
                )

            con.execute(
                "UPDATE drafts SET approval_status = 'publishing', last_publish_error = '', updated_at = ? WHERE id = ?",
                (now, draft_id),
            )
            con.commit()
            return prev_status
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def release_publish_lock(
        self,
        draft_id: int,
        previous_status: str,
        *,
        mark_failed: bool = False,
        error_message: str = "",
    ) -> None:
        """Restore approval_status (or mark 'failed') if a publish attempt did not complete."""
        now = self.now_iso()
        next_status = "failed" if mark_failed else normalize_approval_status(previous_status)
        with self.connect() as con:
            con.execute(
                """
                UPDATE drafts
                SET approval_status = ?,
                    last_publish_error = ?,
                    last_error_at = ?,
                    updated_at = ?
                WHERE id = ? AND approval_status = 'publishing'
                """,
                (
                    next_status,
                    error_message,
                    now if error_message else "",
                    now,
                    draft_id,
                ),
            )

    def get_publish_logs_for_draft(self, draft_id: int) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM publish_logs WHERE draft_id = ? ORDER BY id DESC",
                (draft_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def record_export(self, draft_id: int, export_path: str) -> int:
        """Record an offline bundle export without marking the draft published."""
        now = self.now_iso()
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT approval_status FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            if not row:
                raise ValueError(f"Draft {draft_id} not found.")
            cur_status = normalize_approval_status(str(row["approval_status"]))
            if cur_status == "published":
                raise DuplicatePublishError(f"Draft {draft_id} is already published.")
            if cur_status in ("draft", "pending_approval", "rejected", "failed"):
                raise ApprovalRequiredError(
                    f"Draft {draft_id} has status '{cur_status}' and must be approved before exporting."
                )
            if cur_status not in ("approved", "exported", "scheduled"):
                raise InvalidStateTransitionError(
                    f"Cannot export draft {draft_id} from status '{cur_status}'."
                )

            cur = con.execute(
                """
                INSERT INTO publish_logs (
                    draft_id, mode, outcome, ig_container_id, ig_media_id, permalink, export_path, published_at
                )
                VALUES (?, 'export', 'exported', '', '', '', ?, ?)
                """,
                (draft_id, export_path, now),
            )
            has_queued = (
                con.execute(
                    "SELECT 1 FROM schedules WHERE draft_id = ? AND status = 'queued' LIMIT 1",
                    (draft_id,),
                ).fetchone()
                is not None
            )
            next_status = "scheduled" if (cur_status == "scheduled" and has_queued) else "exported"
            con.execute(
                """
                UPDATE drafts
                SET approval_status = ?, export_path = ?, last_publish_error = '', updated_at = ?
                WHERE id = ?
                """,
                (next_status, export_path, now, draft_id),
            )
            con.commit()
            return int(cur.lastrowid)
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def record_staged(self, draft_id: int, mode: str = "ig_browser_stage", notes: str = "") -> int:
        """Record that an approved draft was staged in IG web composer and awaits human Share confirmation."""
        now = self.now_iso()
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT approval_status FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            if not row:
                raise ValueError(f"Draft {draft_id} not found.")
            cur_status = normalize_approval_status(str(row["approval_status"]))
            if cur_status == "published":
                raise DuplicatePublishError(f"Draft {draft_id} is already published.")
            if cur_status in ("draft", "pending_approval", "rejected", "failed"):
                raise ApprovalRequiredError(
                    f"Draft {draft_id} has status '{cur_status}' and must be approved before staging."
                )
            if cur_status not in ("approved", "exported", "scheduled", "publishing"):
                raise InvalidStateTransitionError(
                    f"Cannot stage draft {draft_id} from status '{cur_status}'."
                )

            cur = con.execute(
                """
                INSERT INTO publish_logs (
                    draft_id, mode, outcome, ig_container_id, ig_media_id, permalink, export_path, published_at
                )
                VALUES (?, ?, 'staged_awaiting_confirmation', '', '', '', ?, ?)
                """,
                (draft_id, mode, notes, now),
            )
            con.execute(
                """
                UPDATE drafts
                SET approval_status = 'staged', staged_at = ?, last_publish_error = '', updated_at = ?
                WHERE id = ?
                """,
                (now, now, draft_id),
            )
            con.execute(
                "UPDATE schedules SET status = 'staged', updated_at = ? WHERE draft_id = ? AND status IN ('queued', 'running')",
                (now, draft_id),
            )
            con.commit()
            return int(cur.lastrowid)
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def confirm_staged_publish(
        self, draft_id: int, ig_media_id: str = "", permalink: str = ""
    ) -> int:
        """Promote a 'staged' draft to 'published' only after explicit operator confirmation."""
        now = self.now_iso()
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT approval_status FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            if not row:
                raise ValueError(f"Draft {draft_id} not found.")
            cur_status = normalize_approval_status(str(row["approval_status"]))
            if cur_status == "published":
                raise DuplicatePublishError(f"Draft {draft_id} is already published.")
            if cur_status != "staged":
                raise InvalidStateTransitionError(
                    f"Draft {draft_id} is '{cur_status}'; only 'staged' drafts can be confirmed as published."
                )
            cur = con.execute(
                """
                INSERT INTO publish_logs (
                    draft_id, mode, outcome, ig_container_id, ig_media_id, permalink, export_path, published_at
                )
                VALUES (?, 'ig_browser_confirmed', 'published', '', ?, ?, '', ?)
                """,
                (draft_id, ig_media_id, permalink, now),
            )
            con.execute(
                """
                UPDATE drafts
                SET approval_status = 'published', published_at = ?, last_publish_error = '', updated_at = ?
                WHERE id = ?
                """,
                (now, now, draft_id),
            )
            con.execute(
                "UPDATE schedules SET status = 'completed', updated_at = ? WHERE draft_id = ? AND status IN ('queued', 'staged')",
                (now, draft_id),
            )
            con.commit()
            return int(cur.lastrowid)
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def record_publish(
        self,
        draft_id: int,
        mode: str,
        ig_container_id: str = "",
        ig_media_id: str = "",
        permalink: str = "",
        export_path: str = "",
        outcome: str = "published",
    ) -> int:
        now = self.now_iso()
        with self.connect() as con:
            cur = con.execute(
                """
                INSERT INTO publish_logs (
                    draft_id, mode, outcome, ig_container_id, ig_media_id, permalink, export_path, published_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    mode,
                    outcome,
                    ig_container_id,
                    ig_media_id,
                    permalink,
                    export_path,
                    now,
                ),
            )
            con.execute(
                """
                UPDATE drafts
                SET approval_status = 'published',
                    published_at = ?,
                    last_publish_error = '',
                    updated_at = ?
                WHERE id = ?
                """,
                (now, now, draft_id),
            )
            con.execute(
                "UPDATE schedules SET status = 'completed', updated_at = ? WHERE draft_id = ? AND status IN ('queued', 'running')",
                (now, draft_id),
            )
            return int(cur.lastrowid)

    def list_publish_logs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT p.*, d.title AS draft_title, d.format AS draft_format, d.approval_status AS draft_status
                FROM publish_logs p
                JOIN drafts d ON d.id = p.draft_id
                ORDER BY p.id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_due_insight_targets(self, horizon_hours: int, limit: int = 25) -> list[dict[str, Any]]:
        """Return published Graph API posts that do not yet have a snapshot for horizon_hours."""
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT p.*, d.format AS draft_format, d.title AS draft_title
                FROM publish_logs p
                JOIN drafts d ON d.id = p.draft_id
                LEFT JOIN insights i ON i.publish_log_id = p.id AND i.horizon_hours = ?
                WHERE p.outcome = 'published'
                  AND p.ig_media_id != ''
                  AND i.id IS NULL
                ORDER BY p.published_at ASC
                LIMIT ?
                """,
                (horizon_hours, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def upsert_media_insight(self, publish_log_id: int, snapshot: InsightSnapshot) -> int:
        """Persist or refresh a horizon snapshot preserving nullable metrics and raw diagnostics."""
        now = self.now_iso()
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO insights (
                    publish_log_id, horizon_hours, reach, views, plays, likes, comments,
                    saves, shares, profile_visits, follows, avg_watch_time_ms,
                    engagement_score, source, raw_metrics_json, unsupported_metrics_json, captured_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(publish_log_id, horizon_hours) DO UPDATE SET
                    reach = excluded.reach,
                    views = excluded.views,
                    plays = excluded.plays,
                    likes = excluded.likes,
                    comments = excluded.comments,
                    saves = excluded.saves,
                    shares = excluded.shares,
                    profile_visits = excluded.profile_visits,
                    follows = excluded.follows,
                    avg_watch_time_ms = excluded.avg_watch_time_ms,
                    engagement_score = excluded.engagement_score,
                    source = excluded.source,
                    raw_metrics_json = excluded.raw_metrics_json,
                    unsupported_metrics_json = excluded.unsupported_metrics_json,
                    captured_at = excluded.captured_at
                """,
                (
                    publish_log_id,
                    snapshot.horizon_hours,
                    snapshot.reach,
                    snapshot.views,
                    snapshot.plays,
                    snapshot.likes,
                    snapshot.comments,
                    snapshot.saves,
                    snapshot.shares,
                    snapshot.profile_visits,
                    snapshot.follows,
                    snapshot.avg_watch_time_ms,
                    snapshot.engagement_score,
                    snapshot.source,
                    json.dumps(snapshot.raw_metrics),
                    json.dumps(snapshot.unsupported_metrics),
                    now,
                ),
            )
            row = con.execute(
                "SELECT id FROM insights WHERE publish_log_id = ? AND horizon_hours = ?",
                (publish_log_id, snapshot.horizon_hours),
            ).fetchone()
            return int(row["id"])

    def insert_insight(
        self,
        publish_log_id: int,
        horizon_hours: int,
        reach: int | None,
        plays: int | None,
        likes: int | None,
        comments: int | None,
        saves: int | None,
        shares: int | None,
        engagement_score: float | None,
    ) -> int:
        snap = InsightSnapshot(
            horizon_hours=horizon_hours,
            reach=reach,
            plays=plays,
            likes=likes,
            comments=comments,
            saves=saves,
            shares=shares,
            source="manual",
        )
        # Keep caller-provided engagement_score when passed explicitly
        now = self.now_iso()
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO insights (
                    publish_log_id, horizon_hours, reach, plays, likes, comments,
                    saves, shares, engagement_score, source, raw_metrics_json, unsupported_metrics_json, captured_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', '{}', '[]', ?)
                ON CONFLICT(publish_log_id, horizon_hours) DO UPDATE SET
                    reach = excluded.reach,
                    plays = excluded.plays,
                    likes = excluded.likes,
                    comments = excluded.comments,
                    saves = excluded.saves,
                    shares = excluded.shares,
                    engagement_score = excluded.engagement_score,
                    source = excluded.source,
                    captured_at = excluded.captured_at
                """,
                (
                    publish_log_id,
                    snap.horizon_hours,
                    reach,
                    plays,
                    likes,
                    comments,
                    saves,
                    shares,
                    engagement_score,
                    now,
                ),
            )
            row = con.execute(
                "SELECT id FROM insights WHERE publish_log_id = ? AND horizon_hours = ?",
                (publish_log_id, horizon_hours),
            ).fetchone()
            return int(row["id"])

    def list_top_insights(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT i.*, d.title, d.format, d.caption
                FROM insights i
                JOIN publish_logs p ON p.id = i.publish_log_id
                JOIN drafts d ON d.id = p.draft_id
                WHERE p.outcome = 'published'
                ORDER BY COALESCE(i.engagement_score, -1) DESC, i.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_insights_detailed(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT i.*, p.draft_id, p.ig_media_id, p.permalink, p.published_at,
                       d.title, d.format, t.pillar
                FROM insights i
                JOIN publish_logs p ON p.id = i.publish_log_id
                JOIN drafts d ON d.id = p.draft_id
                LEFT JOIN topics t ON t.id = d.topic_id
                ORDER BY i.captured_at DESC, i.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def insert_account_insight(self, result: AccountInsightResult) -> int:
        with self.connect() as con:
            cur = con.execute(
                """
                INSERT INTO account_insight_snapshots (
                    ig_user_id, window_days, reach, views, profile_views, accounts_engaged,
                    total_interactions, follower_delta, raw_metrics_json,
                    unsupported_metrics_json, metric_errors_json, source, captured_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.ig_user_id,
                    result.window_days,
                    result.reach,
                    result.views,
                    result.profile_views,
                    result.accounts_engaged,
                    result.total_interactions,
                    result.follower_delta,
                    json.dumps(result.raw_metrics),
                    json.dumps(result.unsupported_metrics),
                    json.dumps(result.metric_errors),
                    result.source,
                    result.captured_at or self.now_iso(),
                ),
            )
            return int(cur.lastrowid)

    def list_account_insights(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM account_insight_snapshots ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def insert_account_snapshot(
        self,
        *,
        brand_slug: str,
        ig_user_id: str,
        handle: str,
        display_name: str,
        biography: str,
        followers_count: int | None,
        follows_count: int | None,
        media_count: int | None,
        website: str,
        source: str,
        raw_json: str,
        captured_at: str,
    ) -> int:
        with self.connect() as con:
            cur = con.execute(
                """
                INSERT INTO account_snapshots (
                    brand_slug, ig_user_id, handle, display_name, biography,
                    followers_count, follows_count, media_count, website,
                    source, raw_json, captured_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    brand_slug,
                    ig_user_id,
                    handle,
                    display_name,
                    biography,
                    followers_count,
                    follows_count,
                    media_count,
                    website,
                    source,
                    raw_json,
                    captured_at,
                ),
            )
            return int(cur.lastrowid)

    def upsert_account_media_cache(
        self,
        *,
        snapshot_id: int | None,
        handle: str,
        items: list[dict[str, Any]],
        source: str,
        captured_at: str,
    ) -> int:
        count = 0
        with self.connect() as con:
            for item in items:
                ig_media_id = str(item.get("ig_media_id") or "")
                con.execute(
                    """
                    INSERT INTO account_media_cache (
                        snapshot_id, handle, ig_media_id, media_type, media_product_type,
                        caption, permalink, like_count, comments_count, reach, views, saves, shares,
                        media_timestamp, source, raw_json, captured_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(handle, ig_media_id) WHERE ig_media_id != '' DO UPDATE SET
                        snapshot_id = excluded.snapshot_id,
                        media_type = excluded.media_type,
                        media_product_type = excluded.media_product_type,
                        caption = excluded.caption,
                        permalink = excluded.permalink,
                        like_count = excluded.like_count,
                        comments_count = excluded.comments_count,
                        reach = COALESCE(excluded.reach, account_media_cache.reach),
                        views = COALESCE(excluded.views, account_media_cache.views),
                        saves = COALESCE(excluded.saves, account_media_cache.saves),
                        shares = COALESCE(excluded.shares, account_media_cache.shares),
                        media_timestamp = excluded.media_timestamp,
                        source = excluded.source,
                        raw_json = excluded.raw_json,
                        captured_at = excluded.captured_at
                    """,
                    (
                        snapshot_id,
                        handle,
                        ig_media_id,
                        str(item.get("media_type") or ""),
                        str(item.get("media_product_type") or ""),
                        str(item.get("caption") or ""),
                        str(item.get("permalink") or ""),
                        item.get("like_count"),
                        item.get("comments_count"),
                        item.get("reach"),
                        item.get("views"),
                        item.get("saves"),
                        item.get("shares"),
                        str(item.get("media_timestamp") or ""),
                        source,
                        json.dumps(item.get("raw") or {}),
                        captured_at,
                    ),
                )
                count += 1
        return count

    def get_latest_account_snapshot(
        self, handle: str | None = None, brand_slug: str | None = None
    ) -> dict[str, Any] | None:
        query = "SELECT * FROM account_snapshots WHERE 1=1"
        params: list[Any] = []
        if handle:
            query += " AND handle = ?"
            params.append(handle)
        if brand_slug:
            query += " AND brand_slug = ?"
            params.append(brand_slug)
        query += " ORDER BY id DESC LIMIT 1"
        with self.connect() as con:
            row = con.execute(query, params).fetchone()
            return dict(row) if row else None

    def list_account_media_cache(self, handle: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        query = "SELECT * FROM account_media_cache"
        params: list[Any] = []
        if handle:
            query += " WHERE handle = ?"
            params.append(handle)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as con:
            rows = con.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Growth Experiment Engine Persistence
    # ------------------------------------------------------------------

    def create_experiment(
        self,
        *,
        name: str,
        hypothesis: str,
        variable_name: str,
        control_value: str,
        treatment_value: str,
        primary_metric: str = "engagement_score",
        min_sample_per_arm: int = 5,
    ) -> int:
        now = self.now_iso()
        with self.connect() as con:
            cur = con.execute(
                """
                INSERT INTO experiments (
                    name, hypothesis, variable_name, control_value, treatment_value,
                    primary_metric, min_sample_per_arm, status, winner, confidence,
                    notes, approved_by_operator, approved_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active', '', 'none', '', 0, '', ?, ?)
                """,
                (
                    name,
                    hypothesis,
                    variable_name,
                    control_value,
                    treatment_value,
                    primary_metric,
                    max(1, min_sample_per_arm),
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def get_experiment(self, experiment_id: int) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM experiments WHERE id = ?", (experiment_id,)).fetchone()
            return dict(row) if row else None

    def list_experiments(self, status: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        query = "SELECT * FROM experiments"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as con:
            rows = con.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def assign_draft_to_experiment(
        self, experiment_id: int, draft_id: int, arm: str, variant_value: str
    ) -> int:
        arm_clean = arm.strip().lower()
        if arm_clean not in ("control", "treatment"):
            raise ValueError("Experiment arm must be 'control' or 'treatment'.")
        now = self.now_iso()
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO experiment_assignments (experiment_id, draft_id, arm, variant_value, assigned_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(experiment_id, draft_id) DO UPDATE SET
                    arm = excluded.arm,
                    variant_value = excluded.variant_value,
                    assigned_at = excluded.assigned_at
                """,
                (experiment_id, draft_id, arm_clean, variant_value, now),
            )
            row = con.execute(
                "SELECT id FROM experiment_assignments WHERE experiment_id = ? AND draft_id = ?",
                (experiment_id, draft_id),
            ).fetchone()
            return int(row["id"])

    def list_experiment_draft_metrics(
        self, experiment_id: int, horizon_hours: int = 24
    ) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT a.experiment_id, a.draft_id, a.arm, a.variant_value, a.assigned_at,
                       d.title, d.format, d.approval_status,
                       p.id AS publish_log_id, p.published_at,
                       i.horizon_hours, i.reach, i.views, i.likes, i.comments, i.saves, i.shares,
                       i.engagement_score
                FROM experiment_assignments a
                JOIN drafts d ON d.id = a.draft_id
                LEFT JOIN publish_logs p ON p.draft_id = a.draft_id AND p.outcome = 'published'
                LEFT JOIN insights i ON i.publish_log_id = p.id AND i.horizon_hours = ?
                WHERE a.experiment_id = ?
                ORDER BY a.id ASC
                """,
                (horizon_hours, experiment_id),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_experiment_evaluation(
        self,
        experiment_id: int,
        *,
        winner: str,
        confidence: str,
        notes: str,
        status: str = "active",
    ) -> None:
        with self.connect() as con:
            con.execute(
                """
                UPDATE experiments
                SET winner = ?, confidence = ?, notes = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (winner, confidence, notes, status, self.now_iso(), experiment_id),
            )

    def save_experiment_learning(
        self,
        *,
        experiment_id: int,
        brand_id: int,
        summary: str,
        recommended_rule: str,
        sample_size_control: int,
        sample_size_treatment: int,
        control_metric_avg: float | None,
        treatment_metric_avg: float | None,
        lift_pct: float | None,
        confidence: str,
        winner: str,
    ) -> int:
        """Create a candidate learning requiring explicit operator approval before prompt promotion."""
        now = self.now_iso()
        with self.connect() as con:
            cur = con.execute(
                """
                INSERT INTO experiment_learnings (
                    experiment_id, brand_id, summary, recommended_rule,
                    sample_size_control, sample_size_treatment,
                    control_metric_avg, treatment_metric_avg, lift_pct,
                    confidence, winner, status, approved_by_operator, promoted_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_approval', 0, '', ?)
                """,
                (
                    experiment_id,
                    brand_id,
                    summary,
                    recommended_rule,
                    sample_size_control,
                    sample_size_treatment,
                    control_metric_avg,
                    treatment_metric_avg,
                    lift_pct,
                    confidence,
                    winner,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def approve_experiment_learning(self, learning_id: int) -> dict[str, Any]:
        now = self.now_iso()
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM experiment_learnings WHERE id = ?", (learning_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Experiment learning {learning_id} not found.")
            con.execute(
                """
                UPDATE experiment_learnings
                SET status = 'approved', approved_by_operator = 1, promoted_at = ?
                WHERE id = ?
                """,
                (now, learning_id),
            )
            con.execute(
                """
                UPDATE experiments
                SET approved_by_operator = 1, approved_at = ?, status = 'completed', updated_at = ?
                WHERE id = ?
                """,
                (now, now, row["experiment_id"]),
            )
            updated = con.execute(
                "SELECT * FROM experiment_learnings WHERE id = ?", (learning_id,)
            ).fetchone()
            return dict(updated)

    def reject_experiment_learning(self, learning_id: int) -> dict[str, Any]:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM experiment_learnings WHERE id = ?", (learning_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Experiment learning {learning_id} not found.")
            con.execute(
                "UPDATE experiment_learnings SET status = 'rejected', approved_by_operator = 0 WHERE id = ?",
                (learning_id,),
            )
            updated = con.execute(
                "SELECT * FROM experiment_learnings WHERE id = ?", (learning_id,)
            ).fetchone()
            return dict(updated)

    def list_experiment_learnings(
        self, status: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        query = """
            SELECT l.*, e.name AS experiment_name, e.variable_name
            FROM experiment_learnings l
            JOIN experiments e ON e.id = l.experiment_id
            WHERE 1=1
        """
        params: list[Any] = []
        if status:
            query += " AND l.status = ?"
            params.append(status)
        query += " ORDER BY l.id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as con:
            rows = con.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def log_agent_run(
        self,
        agent_name: str,
        model: str,
        status: str,
        latency_ms: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        draft_id: int | None = None,
        error_message: str = "",
    ) -> None:
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO agent_runs (
                    agent_name, draft_id, model, tokens_in, tokens_out,
                    latency_ms, status, error_message, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_name,
                    draft_id,
                    model,
                    tokens_in,
                    tokens_out,
                    latency_ms,
                    status,
                    error_message,
                    self.now_iso(),
                ),
            )

    def save_account_audit(self, brand_slug: str, report_json: str) -> int:
        with self.connect() as con:
            cur = con.execute(
                """
                INSERT INTO account_audits (brand_slug, report_json, created_at)
                VALUES (?, ?, ?)
                """,
                (brand_slug, report_json, self.now_iso()),
            )
            return int(cur.lastrowid)

    def get_latest_account_audit(self, brand_slug: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                """
                SELECT * FROM account_audits
                WHERE brand_slug = ?
                ORDER BY id DESC LIMIT 1
                """,
                (brand_slug,),
            ).fetchone()
            if not row:
                return None
            data = json.loads(row["report_json"])
            data["captured_at"] = row["created_at"]
            return data

    def dashboard_stats(self) -> dict[str, Any]:
        with self.connect() as con:
            counts = {
                row["approval_status"]: row["cnt"]
                for row in con.execute(
                    "SELECT approval_status, COUNT(*) AS cnt FROM drafts GROUP BY approval_status"
                ).fetchall()
            }
            topics_new = con.execute(
                "SELECT COUNT(*) AS cnt FROM topics WHERE status = 'new'"
            ).fetchone()["cnt"]
            queued_schedules = con.execute(
                "SELECT COUNT(*) AS cnt FROM schedules WHERE status = 'queued'"
            ).fetchone()["cnt"]
            avg_score_row = con.execute(
                "SELECT AVG(engagement_score) AS avg_score FROM insights WHERE engagement_score IS NOT NULL"
            ).fetchone()
            pending_cnt = int(counts.get("pending_approval", 0)) + int(counts.get("pending", 0))
            return {
                "topics_new": int(topics_new),
                "draft": int(counts.get("draft", 0)),
                "pending": pending_cnt,
                "pending_approval": pending_cnt,
                "approved": int(counts.get("approved", 0)),
                "exported": int(counts.get("exported", 0)),
                "scheduled": int(counts.get("scheduled", 0)),
                "staged": int(counts.get("staged", 0)),
                "publishing": int(counts.get("publishing", 0)),
                "rejected": int(counts.get("rejected", 0)),
                "published": int(counts.get("published", 0)),
                "failed": int(counts.get("failed", 0)),
                "queued_schedules": int(queued_schedules),
                "avg_engagement_score": (
                    round(float(avg_score_row["avg_score"]), 2)
                    if avg_score_row and avg_score_row["avg_score"] is not None
                    else None
                ),
            }


def get_database(settings: Settings | None = None) -> Database:
    cfg = settings or Settings()
    db = Database(cfg.db_path(), timezone=cfg.smai_timezone)
    db.init_schema()
    return db
