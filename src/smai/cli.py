"""Typer CLI entrypoint for SMAI."""

from __future__ import annotations

import asyncio
import json

import typer
import uvicorn

from smai.agents.account_analyzer import InstagramAccountAnalyzer
from smai.config import Settings, list_brand_slugs, load_brand
from smai.db.database import get_database
from smai.db.models import ContentFormat
from smai.pipeline import SmaiPipeline
from smai.publishing import (
    DuplicatePublishError,
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
    autopilot: bool = typer.Option(
        False, help="Auto-approve, schedule into regular peak windows, and export bundles."
    ),
) -> None:
    """Run full 5-step cycle (Research -> Suggest -> Audience/Time -> All Formats -> Drafts/Autopilot)."""
    settings = Settings()
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)
    results = asyncio.run(
        pipeline.run_full_draft_cycle(
            brand_slug=brand,
            dry_run_reel=dry_run_reel,
            auto_schedule_and_export=autopilot,
        )
    )
    summary = [
        {
            "draft_id": r["draft"]["id"],
            "format": r["draft"]["format"],
            "title": r["draft"]["title"],
            "target_audience": r["draft"]["target_audience"],
            "recommended_time": r["draft"]["recommended_time"],
            "qa_status": r["qa"]["status"],
        }
        for r in results
    ]
    typer.echo(json.dumps(summary, indent=2))


@app.command()
def publish(
    draft_id: int = typer.Argument(..., help="Draft ID to publish or export."),
    mode: str = typer.Option("", help="Override mode: export | browser | browser_share | graph_api"),
) -> None:
    """Publish via Meta Graph API or export a local bundle."""
    settings = Settings()
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)
    try:
        res = asyncio.run(pipeline.publish_draft(draft_id, mode=mode or None))
        typer.echo(json.dumps(res, indent=2))
    except (DuplicatePublishError, MetaAPIError, ValueError, RuntimeError) as exc:
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
