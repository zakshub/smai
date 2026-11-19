"""Analyst Agent: v27.0 Performance Intelligence and Growth Experiment Engine."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from smai.db.database import Database
from smai.db.models import InsightSnapshot


class AnalystAgent:
    """Scores published post performance, tracks 24h/72h breakdowns, and runs deterministic A/B experiments."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def record_snapshot(self, publish_log_id: int, snapshot: InsightSnapshot) -> int:
        return self.db.upsert_media_insight(publish_log_id=publish_log_id, snapshot=snapshot)

    @staticmethod
    def _mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 2) if values else None

    def get_performance_breakdown(self, limit: int = 100) -> dict[str, Any]:
        """Compute honest 24h/72h, format, pillar, and publish-hour breakdowns without fabricating 0s."""
        rows = self.db.list_insights_detailed(limit=limit)
        account_rows = self.db.list_account_insights(limit=10)

        horizons: dict[int, list[dict[str, Any]]] = {24: [], 72: []}
        by_format: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_pillar: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_hour: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for r in rows:
            hz = int(r.get("horizon_hours") or 24)
            horizons.setdefault(hz, []).append(r)
            by_format[str(r.get("format") or "unknown")].append(r)
            by_pillar[str(r.get("pillar") or "untagged")].append(r)
            pub_at = str(r.get("published_at") or "")
            hour_label = "unknown"
            if "T" in pub_at:
                try:
                    hour_part = pub_at.split("T", 1)[1][:2]
                    if hour_part.isdigit():
                        hour_label = f"{hour_part}:00"
                except Exception:
                    pass
            by_hour[hour_label].append(r)

        def _summarize(group: list[dict[str, Any]]) -> dict[str, Any]:
            scores = [float(x["engagement_score"]) for x in group if x.get("engagement_score") is not None]
            reaches = [float(x["reach"]) for x in group if x.get("reach") is not None]
            saves = [float(x["saves"]) for x in group if x.get("saves") is not None]
            shares = [float(x["shares"]) for x in group if x.get("shares") is not None]
            return {
                "count": len(group),
                "scored_count": len(scores),
                "avg_engagement_score": self._mean(scores),
                "avg_reach": self._mean(reaches),
                "avg_saves": self._mean(saves),
                "avg_shares": self._mean(shares),
            }

        return {
            "total_snapshots": len(rows),
            "horizons": {str(hz): _summarize(items) for hz, items in horizons.items()},
            "by_format": {k: _summarize(v) for k, v in sorted(by_format.items())},
            "by_pillar": {k: _summarize(v) for k, v in sorted(by_pillar.items())},
            "by_publish_hour": {k: _summarize(v) for k, v in sorted(by_hour.items())},
            "account_insights": account_rows,
            "recent_media_insights": rows[:20],
        }

    # ------------------------------------------------------------------
    # Growth Experiment Engine (single-variable control vs treatment)
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
        return self.db.create_experiment(
            name=name,
            hypothesis=hypothesis,
            variable_name=variable_name,
            control_value=control_value,
            treatment_value=treatment_value,
            primary_metric=primary_metric,
            min_sample_per_arm=min_sample_per_arm,
        )

    def assign_draft(
        self,
        experiment_id: int,
        draft_id: int,
        arm: str,
        variant_value: str | None = None,
    ) -> int:
        exp = self.db.get_experiment(experiment_id)
        if not exp:
            raise ValueError(f"Experiment {experiment_id} not found.")
        arm_clean = arm.strip().lower()
        val = variant_value or (
            exp["control_value"] if arm_clean == "control" else exp["treatment_value"]
        )
        return self.db.assign_draft_to_experiment(experiment_id, draft_id, arm_clean, val)

    @staticmethod
    def _extract_metric_value(row: dict[str, Any], primary_metric: str) -> float | None:
        if primary_metric in ("save_rate", "share_rate"):
            reach = row.get("reach")
            target = row.get("saves") if primary_metric == "save_rate" else row.get("shares")
            if reach is None or reach <= 0 or target is None:
                return None
            return round((float(target) / float(reach)) * 100.0, 4)
        val = row.get(primary_metric)
        return float(val) if val is not None else None

    def evaluate_experiment(
        self,
        experiment_id: int,
        *,
        horizon_hours: int = 24,
        brand_id: int = 1,
        create_learning_candidate: bool = True,
    ) -> dict[str, Any]:
        """Deterministically evaluate control vs treatment with small-sample warnings and no auto-promotion."""
        exp = self.db.get_experiment(experiment_id)
        if not exp:
            raise ValueError(f"Experiment {experiment_id} not found.")

        rows = self.db.list_experiment_draft_metrics(experiment_id, horizon_hours=horizon_hours)
        primary_metric = str(exp.get("primary_metric") or "engagement_score")
        min_req = max(1, int(exp.get("min_sample_per_arm") or 5))

        ctrl_rows = [r for r in rows if r["arm"] == "control"]
        treat_rows = [r for r in rows if r["arm"] == "treatment"]
        ctrl_vals = [
            v for r in ctrl_rows if (v := self._extract_metric_value(r, primary_metric)) is not None
        ]
        treat_vals = [
            v for r in treat_rows if (v := self._extract_metric_value(r, primary_metric)) is not None
        ]

        n_ctrl = len(ctrl_vals)
        n_treat = len(treat_vals)
        ctrl_avg = round(sum(ctrl_vals) / n_ctrl, 3) if n_ctrl else None
        treat_avg = round(sum(treat_vals) / n_treat, 3) if n_treat else None

        lift_pct: float | None = None
        if ctrl_avg is not None and treat_avg is not None and ctrl_avg > 0:
            lift_pct = round(((treat_avg - ctrl_avg) / ctrl_avg) * 100.0, 2)

        provisional_leader = "inconclusive"
        if ctrl_avg is not None and treat_avg is not None:
            if abs(treat_avg - ctrl_avg) <= 1e-6:
                provisional_leader = "tie"
            else:
                provisional_leader = "treatment" if treat_avg > ctrl_avg else "control"

        is_exploratory = (n_ctrl < min_req) or (n_treat < min_req)
        warnings: list[str] = []
        if n_ctrl == 0 or n_treat == 0:
            winner = "inconclusive"
            confidence = "none"
            warnings.append(
                f"Need observed {horizon_hours}h insights in both arms (control n={n_ctrl}, treatment n={n_treat})."
            )
        elif is_exploratory:
            winner = "inconclusive"
            confidence = "low"
            warnings.append(
                f"Exploratory sample warning: control n={n_ctrl}, treatment n={n_treat} "
                f"(below threshold of {min_req} per arm). Provisional leader: {provisional_leader}."
            )
        else:
            min_n = min(n_ctrl, n_treat)
            abs_lift = abs(lift_pct or 0.0)
            if min_n >= 10 and abs_lift >= 15.0:
                confidence = "high"
            elif abs_lift >= 5.0:
                confidence = "medium"
            else:
                confidence = "low"
            winner = "tie" if provisional_leader == "tie" else provisional_leader

        summary = (
            f"[{exp['name']}] variable='{exp['variable_name']}' | metric='{primary_metric}' ({horizon_hours}h): "
            f"control='{exp['control_value']}' avg={ctrl_avg} (n={n_ctrl}) vs "
            f"treatment='{exp['treatment_value']}' avg={treat_avg} (n={n_treat}); "
            f"lift={lift_pct}%, winner={winner}, confidence={confidence}."
        )
        recommended_rule = (
            f"Prefer '{exp['treatment_value']}' over '{exp['control_value']}' for {exp['variable_name']} "
            f"if confirmed by operator review."
            if provisional_leader == "treatment"
            else f"Keep baseline '{exp['control_value']}' for {exp['variable_name']} until treatment outperforms."
        )

        notes = " | ".join(warnings) if warnings else "Sample threshold satisfied."
        self.db.update_experiment_evaluation(
            experiment_id,
            winner=winner,
            confidence=confidence,
            notes=notes,
            status="active",
        )

        learning_id: int | None = None
        if create_learning_candidate and n_ctrl > 0 and n_treat > 0:
            learning_id = self.db.save_experiment_learning(
                experiment_id=experiment_id,
                brand_id=brand_id,
                summary=summary,
                recommended_rule=recommended_rule,
                sample_size_control=n_ctrl,
                sample_size_treatment=n_treat,
                control_metric_avg=ctrl_avg,
                treatment_metric_avg=treat_avg,
                lift_pct=lift_pct,
                confidence=confidence,
                winner=winner,
            )

        return {
            "experiment": self.db.get_experiment(experiment_id),
            "horizon_hours": horizon_hours,
            "primary_metric": primary_metric,
            "assigned_control": len(ctrl_rows),
            "assigned_treatment": len(treat_rows),
            "observed_control": n_ctrl,
            "observed_treatment": n_treat,
            "control_avg": ctrl_avg,
            "treatment_avg": treat_avg,
            "lift_pct": lift_pct,
            "provisional_leader": provisional_leader,
            "winner": winner,
            "confidence": confidence,
            "is_exploratory": is_exploratory,
            "warnings": warnings,
            "summary": summary,
            "recommended_rule": recommended_rule,
            "learning_id": learning_id,
            "requires_operator_approval": True,
        }

    def promote_learning(self, learning_id: int) -> dict[str, Any]:
        """Explicit operator approval gate before an experiment learning influences generation prompts."""
        return self.db.approve_experiment_learning(learning_id)

    def reject_learning(self, learning_id: int) -> dict[str, Any]:
        return self.db.reject_experiment_learning(learning_id)

    def build_feedback_summary(self, limit: int = 5) -> str:
        lines: list[str] = []
        top = self.db.list_top_insights(limit=limit)
        scored_top = [r for r in top if r.get("engagement_score") is not None]
        if scored_top:
            lines.append("Top performing historical posts (verified insights, save+share weighted):")
            for row in scored_top:
                lines.append(
                    f"- [{row['format']}] \"{row['title']}\" | score={row['engagement_score']} "
                    f"(saves={row['saves']}, shares={row['shares']}, reach={row['reach']})"
                )

        approved_learnings = self.db.list_experiment_learnings(status="approved", limit=5)
        if approved_learnings:
            lines.append("Operator-approved growth experiment learnings:")
            for lrn in approved_learnings:
                lines.append(
                    f"- [{lrn['experiment_name']}] {lrn['recommended_rule']} "
                    f"(confidence={lrn['confidence']}, lift={lrn['lift_pct']}%)"
                )
        return "\n".join(lines)
