"""Typer CLI entrypoint for SMAI."""

from __future__ import annotations

import asyncio
import json

import typer
import uvicorn

from smai.agents.account_analyzer import InstagramAccountAnalyzer
from smai.config import Settings, list_brand_slugs, load_brand
from smai.db.database import get_database
from smai.db.models import (
    ApprovalRequiredError,
    ContentFormat,
    CycleMode,
    DuplicatePublishError,
    InvalidStateTransitionError,
)
from smai.pipeline import SmaiPipeline
from smai.publishing import (
    InstagramBrowserAutomator,
    InstagramGraphClient,
    MetaAPIError,
)
from smai.web.app import create_app


app = typer.Typer(help="SMAI — Faceless Instagram Automation Agentic CLI")


@app.command()
def init(brand: str = typer.Option("default", help="Brand config slug to initialize.")) -> None:
    """Initialize SQLite schema and sync BrandBrain configuration."""
    settings = Settings()
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)
    brand_cfg, brand_id = pipeline.ensure_brand(brand)
    typer.echo(
        f"Initialized SMAI DB at {settings.db_path()} | Brand '{brand_cfg.name}' (id={brand_id})"
    )


@app.command()
def brands() -> None:
    """List configured BrandBrain profiles."""
    settings = Settings()
    for slug in list_brand_slugs(settings):
        b = load_brand(slug, settings)
        typer.echo(f"- {slug}: {b.name} ({b.handle}) — {b.niche}")


@app.command()
def ig_login(
    wait: int = typer.Option(90, help="Seconds to wait for you to log into Instagram."),
) -> None:
    """Open a visible Chromium browser window so you can log into Instagram once."""
    settings = Settings()
    automator = InstagramBrowserAutomator(settings)
    res = asyncio.run(automator.launch_login_window(wait_seconds=wait))
    typer.echo(json.dumps(res, indent=2))


@app.command()
def ig_check(
    ig_user_id: str = typer.Option("", help="Override IG_USER_ID for connection check."),
) -> None:
    """Verify Meta Graph API token, assigned Instagram accounts, and profile retrieval."""
    settings = Settings()
    client = InstagramGraphClient(settings)
    report = asyncio.run(client.check_connection_health(ig_user_id=ig_user_id or None))
    typer.echo(json.dumps(report, indent=2))
    if not (report["token_valid"] and report["active_account_verified"]):
        raise typer.Exit(code=1)


@app.command()
def analyze(
    handle: str = typer.Argument(..., help="Instagram @handle or profile URL to analyze."),
    niche: str = typer.Option("", help="Optional niche hint."),
    bio: str = typer.Option("", help="Optional current bio text."),
    brand: str = typer.Option("default", help="Brand slug to update."),
) -> None:
    """Analyze an Instagram account, generate an audit report, and auto-tune BrandBrain."""
    settings = Settings()
    db = get_database(settings)
    analyzer = InstagramAccountAnalyzer(settings, db)
    report = asyncio.run(
        analyzer.analyze(handle_or_url=handle, bio_hint=bio, niche_hint=niche)
    )
    analyzer.apply_to_brand(report, brand_slug=brand)
    typer.echo(report.model_dump_json(indent=2))


@app.command()
def scout(
    brand: str = typer.Option("default", help="Brand config slug."),
    limit: int = typer.Option(5, help="Max new topics to discover."),
) -> None:
    """Discover and score trending faceless Instagram topics."""
    settings = Settings()
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)
    topics = asyncio.run(pipeline.run_scout(brand_slug=brand, limit=limit))
    for t in topics:
        typer.echo(f"[{t.viral_score}] {t.title} ({t.pillar})")


@app.command()
def generate(
    fmt: ContentFormat = typer.Option(
        "carousel", "--format", "-f", help="carousel | reel | single_pic | text_post | story"
    ),
    brand: str = typer.Option("default", help="Brand config slug."),
    topic: str = typer.Option("", help="Optional custom topic."),
    render: bool = typer.Option(True, help="Render PNG/MP4/TXT media assets."),
    dry_run_reel: bool = typer.Option(False, help="Skip FFmpeg encoding for fast Reel dry runs."),
) -> None:
    """Generate a full faceless Instagram draft, run QA, and render media."""
    settings = Settings()
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)
    result = asyncio.run(
        pipeline.generate_draft(
            fmt=fmt,
            brand_slug=brand,
            custom_topic=topic.strip() or None,
            render_media=render,
            dry_run_reel=dry_run_reel,
        )
    )
    typer.echo(
        json.dumps(
            {
                "draft_id": result["draft"]["id"],
                "format": result["draft"]["format"],
                "title": result["draft"]["title"],
                "qa_status": result["qa"]["status"],
                "qa_score": result["qa"]["score"],
                "assets": [a["local_path"] for a in result["assets"]],
            },
            indent=2,
        )
    )


@app.command()
def cycle(
    brand: str = typer.Option("default", help="Brand config slug."),
    dry_run_reel: bool = typer.Option(False, help="Skip FFmpeg encoding for fast Reel dry runs."),
    cycle_mode: CycleMode = typer.Option(
        "draft_only", help="draft_only | export_review | auto_schedule"
    ),
    approve_qa_passed: bool = typer.Option(
        False, help="Explicitly approve QA-passed drafts before export_review/auto_schedule."
    ),
) -> None:
    """Run research-to-draft cycle cleanly without mixing scheduling and immediate publish/export."""
    settings = Settings()
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)
    results = asyncio.run(
        pipeline.run_full_draft_cycle(
            brand_slug=brand,
            dry_run_reel=dry_run_reel,
            cycle_mode=cycle_mode,
            approve_qa_passed=approve_qa_passed,
        )
    )
    summary = [
        {
            "draft_id": r["draft"]["id"],
            "format": r["draft"]["format"],
            "title": r["draft"]["title"],
            "approval_status": r["draft"]["approval_status"],
            "qa_status": r["qa"]["status"],
            "cycle_note": r.get("cycle_note", ""),
        }
        for r in results
    ]
    typer.echo(json.dumps(summary, indent=2))


@app.command()
def approve(draft_id: int = typer.Argument(..., help="Draft ID to approve.")) -> None:
    """Explicitly approve a draft for export, scheduling, staging, or publishing."""
    settings = Settings()
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)
    try:
        updated = pipeline.approve_draft(draft_id)
        typer.echo(json.dumps({"id": updated["id"], "approval_status": updated["approval_status"]}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(code=1)


@app.command()
def reject(draft_id: int = typer.Argument(..., help="Draft ID to reject.")) -> None:
    """Reject a draft and cancel any active queued schedules."""
    settings = Settings()
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)
    try:
        updated = pipeline.reject_draft(draft_id)
        typer.echo(json.dumps({"id": updated["id"], "approval_status": updated["approval_status"]}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(code=1)


@app.command()
def reopen(
    draft_id: int = typer.Argument(..., help="Draft ID to reopen."),
    status: str = typer.Option("pending_approval", help="Target status: draft | pending_approval | approved"),
) -> None:
    """Reopen a rejected, failed, or staged draft (never published)."""
    settings = Settings()
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)
    try:
        updated = pipeline.reopen_draft(draft_id, target_status=status)
        typer.echo(json.dumps({"id": updated["id"], "approval_status": updated["approval_status"]}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(code=1)


@app.command("export")
def export_cmd(draft_id: int = typer.Argument(..., help="Approved draft ID to export.")) -> None:
    """Export a local media + caption bundle without marking published."""
    settings = Settings()
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)
    try:
        res = asyncio.run(pipeline.export_draft(draft_id))
        typer.echo(json.dumps({"publish_log_id": res["publish_log_id"], "export_path": res["export_path"], "approval_status": res["draft"]["approval_status"]}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(code=1)


@app.command()
def schedule(
    draft_id: int = typer.Argument(..., help="Approved draft ID to schedule."),
    at: str = typer.Option("", help="Optional ISO-8601 timestamp; defaults to next jittered window."),
) -> None:
    """Schedule an approved draft into a posting window."""
    settings = Settings()
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)
    try:
        res = pipeline.schedule_draft(draft_id, scheduled_for=at.strip() or None)
        typer.echo(json.dumps({"schedule_id": res["schedule_id"], "scheduled_for": res["scheduled_for"], "approval_status": res["draft"]["approval_status"]}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(code=1)


@app.command("cancel-schedule")
def cancel_schedule_cmd(schedule_id: int = typer.Argument(..., help="Queued schedule ID to cancel.")) -> None:
    """Cancel a queued schedule and restore draft status."""
    settings = Settings()
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)
    try:
        res = pipeline.cancel_schedule(schedule_id)
        typer.echo(json.dumps(res, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(code=1)


@app.command()
def stage(draft_id: int = typer.Argument(..., help="Approved draft ID to stage in IG Web Composer.")) -> None:
    """Stage media and caption in Instagram browser composer (records 'staged', awaiting confirmation)."""
    settings = Settings()
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)
    try:
        res = asyncio.run(pipeline.stage_in_browser(draft_id))
        typer.echo(json.dumps(res, indent=2, default=str))
    except Exception as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(code=1)


@app.command("confirm-publish")
def confirm_publish_cmd(
    draft_id: int = typer.Argument(..., help="Staged draft ID to confirm as manually shared."),
    media_id: str = typer.Option("", help="Optional IG media ID."),
    permalink: str = typer.Option("", help="Optional Instagram post URL."),
) -> None:
    """Confirm a staged browser upload was shared manually by the operator."""
    settings = Settings()
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)
    try:
        res = pipeline.confirm_staged_publish(draft_id, ig_media_id=media_id, permalink=permalink)
        typer.echo(json.dumps(res, indent=2, default=str))
    except Exception as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(code=1)


@app.command()
def publish(
    draft_id: int = typer.Argument(..., help="Approved draft ID to publish live."),
    mode: str = typer.Option("", help="Override live mode: graph_api | browser_share"),
    skip_url_check: bool = typer.Option(False, help="Skip HEAD/GET public media preflight check."),
) -> None:
    """Publish an approved draft via live Meta Graph API or confirmed browser_share."""
    settings = Settings()
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)
    try:
        res = asyncio.run(
            pipeline.publish_draft(
                draft_id,
                mode=mode or None,
                verify_public_media=not skip_url_check,
            )
        )
        typer.echo(json.dumps(res, indent=2))
    except (
        ApprovalRequiredError,
        DuplicatePublishError,
        InvalidStateTransitionError,
        MetaAPIError,
        ValueError,
        RuntimeError,
    ) as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(code=1)


@app.command("insights-sync")
def insights_sync_cmd(window_days: int = typer.Option(1, help="Account insights lookback days.")) -> None:
    """Sync live Meta Graph API v27.0 account & published media insights."""
    settings = Settings()
    if not settings.has_meta_credentials():
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "blocked": "Missing IG_ACCESS_TOKEN or IG_USER_ID in environment/.env.",
                },
                indent=2,
            ),
            err=True,
        )
        raise typer.Exit(code=1)
    db = get_database(settings)
    client = InstagramGraphClient(settings)
    pipeline = SmaiPipeline(settings, db, graph_client=client)
    try:
        acct = asyncio.run(client.fetch_account_insights_safe(window_days=window_days))
        db.insert_account_insight(acct)
        media_synced = 0
        for row in db.list_publish_logs(limit=15):
            if row.get("outcome") != "published" or not row.get("ig_media_id"):
                continue
            fmt = str(row.get("draft_format") or "").lower()
            prod = "REELS" if fmt in ("reel", "slide_video") else ("STORY" if fmt == "story" else "FEED")
            snap = asyncio.run(
                client.fetch_media_insights_safe(
                    str(row["ig_media_id"]), media_product_type=prod, horizon_hours=24
                )
            )
            pipeline.analyst.record_snapshot(int(row["id"]), snap)
            media_synced += 1
        typer.echo(json.dumps({"ok": True, "media_synced": media_synced, "account": acct.model_dump()}, indent=2))
    except MetaAPIError as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}, indent=2), err=True)
        raise typer.Exit(code=1)


@app.command("experiment-create")
def experiment_create_cmd(
    name: str = typer.Option(..., help="Experiment name."),
    hypothesis: str = typer.Option(..., help="Test hypothesis."),
    variable: str = typer.Option(..., help="Single variable tested (e.g. hook_type)."),
    control: str = typer.Option(..., help="Control variant value."),
    treatment: str = typer.Option(..., help="Treatment variant value."),
    metric: str = typer.Option("engagement_score", help="Primary metric."),
    min_sample: int = typer.Option(5, help="Minimum samples per arm before declaring non-exploratory winner."),
) -> None:
    """Create a single-variable control-vs-treatment Growth Experiment."""
    settings = Settings()
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)
    exp_id = pipeline.analyst.create_experiment(
        name=name,
        hypothesis=hypothesis,
        variable_name=variable,
        control_value=control,
        treatment_value=treatment,
        primary_metric=metric,
        min_sample_per_arm=min_sample,
    )
    typer.echo(json.dumps({"experiment_id": exp_id, "name": name}, indent=2))


@app.command("experiment-eval")
def experiment_eval_cmd(
    experiment_id: int = typer.Argument(..., help="Experiment ID to evaluate."),
    horizon_hours: int = typer.Option(24, help="Insight horizon (24 or 72)."),
) -> None:
    """Deterministically evaluate a Growth Experiment (requires explicit operator approval before promotion)."""
    settings = Settings()
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)
    try:
        res = pipeline.analyst.evaluate_experiment(experiment_id, horizon_hours=horizon_hours)
        typer.echo(json.dumps(res, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(code=1)


@app.command("learning-approve")
def learning_approve_cmd(learning_id: int = typer.Argument(..., help="Experiment learning ID to promote.")) -> None:
    """Approve an experiment learning so it informs future content generation."""
    settings = Settings()
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)
    try:
        res = pipeline.analyst.promote_learning(learning_id)
        typer.echo(json.dumps(res, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(code=1)


@app.command()
def status() -> None:
    """Show pipeline queue and analytics summary."""
    settings = Settings()
    db = get_database(settings)
    typer.echo(json.dumps(db.dashboard_stats(), indent=2))


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind host."),
    port: int = typer.Option(8000, help="Bind port."),
    no_scheduler: bool = typer.Option(False, help="Disable background scheduler."),
) -> None:
    """Launch the SMAI local Studio Dashboard."""
    settings = Settings()
    app_instance = create_app(settings=settings, enable_scheduler=not no_scheduler)
    uvicorn.run(app_instance, host=host, port=port)


if __name__ == "__main__":
    app()
