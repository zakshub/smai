"""Analyst Agent: summarizes winning post patterns from IG Insights."""

from __future__ import annotations

from smai.db.database import Database
from smai.db.models import InsightSnapshot


class AnalystAgent:
    """Scores published post performance and distills prompt feedback."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def record_snapshot(self, publish_log_id: int, snapshot: InsightSnapshot) -> int:
        return self.db.insert_insight(
            publish_log_id=publish_log_id,
            horizon_hours=snapshot.horizon_hours,
            reach=snapshot.reach,
            plays=snapshot.plays,
            likes=snapshot.likes,
            comments=snapshot.comments,
            saves=snapshot.saves,
            shares=snapshot.shares,
            engagement_score=snapshot.engagement_score,
        )

    def build_feedback_summary(self, limit: int = 5) -> str:
        top = self.db.list_top_insights(limit=limit)
        if not top:
            return ""
        lines = ["Top performing historical posts (ranked by save+share weighted score):"]
        for row in top:
            lines.append(
                f"- [{row['format']}] \"{row['title']}\" | score={row['engagement_score']} "
                f"(saves={row['saves']}, shares={row['shares']}, reach={row['reach']})"
            )
        return "\n".join(lines)
