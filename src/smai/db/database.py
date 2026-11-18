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
    reach INTEGER DEFAULT 0,
    plays INTEGER DEFAULT 0,
    likes INTEGER DEFAULT 0,
    comments INTEGER DEFAULT 0,
    saves INTEGER DEFAULT 0,
    shares INTEGER DEFAULT 0,
    engagement_score REAL DEFAULT 0.0,
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
                },
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'pending', 0.0, '[]', 'pending', ?, ?)
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
        with self.connect() as con:
            con.execute(
                """
                UPDATE drafts
                SET qa_status = ?, qa_score = ?, qa_notes = ?, updated_at = ?
                WHERE id = ?
                """,
                (qa_status, qa_score, json.dumps(qa_notes), self.now_iso(), draft_id),
            )

    def update_draft_content(
        self,
        draft_id: int,
        caption: str | None = None,
        selected_hook_idx: int | None = None,
        script_json: str | None = None,
        approval_status: str | None = None,
    ) -> None:
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
            fields.append("approval_status = ?")
            values.append(approval_status)
        if not fields:
            return
        fields.append("updated_at = ?")
        values.append(self.now_iso())
        values.append(draft_id)
        with self.connect() as con:
            con.execute(
                f"UPDATE drafts SET {', '.join(fields)} WHERE id = ?",
                values,
            )

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
        now = self.now_iso()
        with self.connect() as con:
            cur = con.execute(
                """
                INSERT INTO schedules (draft_id, scheduled_for, status, attempts, last_error, created_at, updated_at)
                VALUES (?, ?, 'queued', 0, '', ?, ?)
                """,
                (draft_id, scheduled_for, now, now),
            )
            return int(cur.lastrowid)

    def list_schedules(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query = """
            SELECT s.*, d.title AS draft_title, d.format AS draft_format
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

    def record_publish(
        self,
        draft_id: int,
        mode: str,
        ig_container_id: str = "",
        ig_media_id: str = "",
        permalink: str = "",
        export_path: str = "",
    ) -> int:
        with self.connect() as con:
            cur = con.execute(
                """
                INSERT INTO publish_logs (
                    draft_id, mode, ig_container_id, ig_media_id, permalink, export_path, published_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    mode,
                    ig_container_id,
                    ig_media_id,
                    permalink,
                    export_path,
                    self.now_iso(),
                ),
            )
            con.execute(
                "UPDATE drafts SET approval_status = 'published', updated_at = ? WHERE id = ?",
                (self.now_iso(), draft_id),
            )
            return int(cur.lastrowid)

    def list_publish_logs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT p.*, d.title AS draft_title, d.format AS draft_format
                FROM publish_logs p
                JOIN drafts d ON d.id = p.draft_id
                ORDER BY p.id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def insert_insight(
        self,
        publish_log_id: int,
        horizon_hours: int,
        reach: int,
        plays: int,
        likes: int,
        comments: int,
        saves: int,
        shares: int,
        engagement_score: float,
    ) -> int:
        with self.connect() as con:
            cur = con.execute(
                """
                INSERT INTO insights (
                    publish_log_id, horizon_hours, reach, plays, likes, comments,
                    saves, shares, engagement_score, captured_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    publish_log_id,
                    horizon_hours,
                    reach,
                    plays,
                    likes,
                    comments,
                    saves,
                    shares,
                    engagement_score,
                    self.now_iso(),
                ),
            )
            return int(cur.lastrowid)

    def list_top_insights(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT i.*, d.title, d.format, d.caption
                FROM insights i
                JOIN publish_logs p ON p.id = i.publish_log_id
                JOIN drafts d ON d.id = p.draft_id
                ORDER BY i.engagement_score DESC, i.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
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
                "SELECT AVG(engagement_score) AS avg_score FROM insights"
            ).fetchone()
            return {
                "topics_new": int(topics_new),
                "pending": int(counts.get("pending", 0)),
                "approved": int(counts.get("approved", 0)),
                "rejected": int(counts.get("rejected", 0)),
                "published": int(counts.get("published", 0)),
                "queued_schedules": int(queued_schedules),
                "avg_engagement_score": round(float(avg_score_row["avg_score"] or 0.0), 2),
            }


def get_database(settings: Settings | None = None) -> Database:
    cfg = settings or Settings()
    db = Database(cfg.db_path(), timezone=cfg.smai_timezone)
    db.init_schema()
    return db
