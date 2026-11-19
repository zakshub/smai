"""FastAPI local studio dashboard and API for SMAI."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from smai.agents.account_analyzer import InstagramAccountAnalyzer
from smai.agents.algorithm import ALGORITHM_FORMAT_PLAYBOOK
from smai.config import Settings, list_brand_slugs, load_brand
from smai.db.database import get_database
from smai.db.models import ContentFormat, InsightSnapshot
from smai.pipeline import SmaiPipeline
from smai.publishing import (
    DuplicatePublishError,
    InstagramBrowserAutomator,
    InstagramGraphClient,
    MetaAPIError,
)
from smai.scheduler import SchedulerWorker, pick_next_window_slot


def create_app(
    settings: Settings | None = None,
    enable_scheduler: bool = True,
) -> FastAPI:
    cfg = settings or Settings()
    db = get_database(cfg)
    worker = SchedulerWorker(cfg, db)
    templates_dir = Path(__file__).resolve().parents[1] / "templates" / "web"
    templates = Jinja2Templates(directory=str(templates_dir))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        pipeline = SmaiPipeline(cfg, db)
        pipeline.ensure_brand()
        if enable_scheduler:
            worker.start()
        try:
            yield
        finally:
            if enable_scheduler:
                worker.stop()

    app = FastAPI(title="SMAI Studio", version="0.1.0", lifespan=lifespan)
    media_root = cfg.media_dir()
    app.mount("/media", StaticFiles(directory=str(media_root)), name="media")

    def _to_media_url(local_path: str) -> str:
        try:
            rel = Path(local_path).resolve().relative_to(media_root.resolve()).as_posix()
            return f"/media/{rel}"
        except Exception:
            return ""

    def _enrich_draft(draft: dict[str, Any]) -> dict[str, Any]:
        assets = db.list_media_assets(int(draft["id"]))
        for asset in assets:
            asset["url"] = _to_media_url(asset["local_path"])
        draft["assets"] = assets
        draft["ab_hooks"] = json.loads(draft.get("ab_hooks_json") or "[]")
        draft["qa_notes_list"] = json.loads(draft.get("qa_notes") or "[]")
        draft["script"] = json.loads(draft.get("script_json") or "{}")
        return draft

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, brand: str = "") -> HTMLResponse:
        available_brands = list_brand_slugs(cfg)
        active_slug = brand if brand in available_brands else cfg.smai_default_brand
        pipeline = SmaiPipeline(cfg, db)
        brand_cfg, brand_id = pipeline.ensure_brand(active_slug)
        drafts = [_enrich_draft(d) for d in db.list_drafts(brand_id=brand_id, limit=30)]
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "brand": brand_cfg,
                "available_brands": available_brands,
                "stats": db.dashboard_stats(),
                "topics": db.list_topics(brand_id, status="new", limit=10),
                "drafts": drafts,
                "schedules": db.list_schedules(limit=15),
                "publish_logs": db.list_publish_logs(limit=10),
                "insights": db.list_top_insights(limit=5),
                "algorithm_playbook": ALGORITHM_FORMAT_PLAYBOOK,
                "account_audit": db.get_latest_account_audit(brand_cfg.slug),
                "llm_provider": f"{cfg.smai_llm_provider} ({cfg.smai_llm_model})",
                "gemini_key_set": bool(cfg.effective_gemini_key()),
                "ig_browser_connected": InstagramBrowserAutomator(cfg).has_saved_session(),
                "meta_api_configured": cfg.has_meta_credentials(),
                "meta_graph_version": cfg.ig_graph_api_version,
                "meta_ig_user_id": cfg.ig_user_id,
                "publish_mode": cfg.smai_publish_mode,
                "project_root_path": str(cfg.project_root.resolve()),
                "media_dir_path": str(cfg.media_dir().resolve()),
                "export_dir_path": str(cfg.resolve_export_dir().resolve()),
            },
        )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "stats": db.dashboard_stats()}

    @app.get("/api/meta/status")
    async def api_meta_status() -> dict[str, Any]:
        """Return non-secret Meta Graph API Phase A connection health report."""
        client = InstagramGraphClient(cfg)
        return await client.check_connection_health()

    @app.get("/api/drafts")
    def api_list_drafts() -> list[dict[str, Any]]:
        return [_enrich_draft(d) for d in db.list_drafts(limit=50)]

    @app.post("/actions/analyze-account")
    async def action_analyze_account(
        handle_or_url: str = Form(...),
        niche_hint: str = Form(""),
        bio_hint: str = Form(""),
        ig_user_id: str = Form(""),
        ig_access_token: str = Form(""),
        gemini_api_key: str = Form(""),
        brand_slug: str = Form("default"),
    ) -> RedirectResponse:
        slug = brand_slug.strip() or cfg.smai_default_brand
        if gemini_api_key.strip():
            cfg.gemini_api_key = gemini_api_key.strip()
            cfg.smai_llm_provider = "gemini"
        analyzer = InstagramAccountAnalyzer(cfg, db)
        report = await analyzer.analyze(
            handle_or_url=handle_or_url,
            bio_hint=bio_hint,
            niche_hint=niche_hint,
            ig_user_id=ig_user_id,
            ig_access_token=ig_access_token,
            enable_network=cfg.smai_enable_network_scout,
        )
        analyzer.apply_to_brand(
            report=report,
            brand_slug=slug,
            ig_user_id=ig_user_id,
            ig_access_token=ig_access_token,
            gemini_api_key=gemini_api_key,
        )
        pipeline = SmaiPipeline(cfg, db)
        await pipeline.run_scout(brand_slug=slug, limit=5)
        return RedirectResponse(url=f"/?brand={slug}", status_code=303)

    @app.post("/actions/scout")
    async def action_scout(brand_slug: str = Form("")) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        slug = brand_slug.strip() or None
        await pipeline.run_scout(brand_slug=slug, limit=5)
        return RedirectResponse(url=f"/?brand={slug}" if slug else "/", status_code=303)

    @app.post("/actions/cycle")
    async def action_full_cycle(
        brand_slug: str = Form(""),
        autopilot: str = Form(""),
    ) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        slug = brand_slug.strip() or None
        await pipeline.run_full_draft_cycle(
            brand_slug=slug,
            render_media=True,
            auto_schedule_and_export=(autopilot == "1"),
        )
        return RedirectResponse(url=f"/?brand={slug}" if slug else "/", status_code=303)

    @app.post("/actions/generate")
    async def action_generate(
        fmt: ContentFormat = Form("carousel"),
        brand_slug: str = Form(""),
        topic_id: str = Form(""),
        custom_topic: str = Form(""),
    ) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        slug = brand_slug.strip() or None
        tid = int(topic_id) if topic_id.strip().isdigit() else None
        await pipeline.generate_draft(
            fmt=fmt,
            brand_slug=slug,
            topic_id=tid,
            custom_topic=custom_topic.strip() or None,
            render_media=True,
        )
        return RedirectResponse(url=f"/?brand={slug}" if slug else "/", status_code=303)

    @app.post("/drafts/{draft_id}/hook")
    def action_select_hook(draft_id: int, hook_idx: int = Form(0)) -> RedirectResponse:
        draft = db.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        db.update_draft_content(draft_id, selected_hook_idx=hook_idx)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/drafts/{draft_id}/caption")
    def action_update_caption(draft_id: int, caption: str = Form(...)) -> RedirectResponse:
        draft = db.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        db.update_draft_content(draft_id, caption=caption)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/drafts/{draft_id}/approve")
    def action_approve(draft_id: int) -> RedirectResponse:
        draft = db.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        brand = load_brand(cfg.smai_default_brand, cfg)
        slot = pick_next_window_slot(brand.schedule_windows, timezone=brand.timezone)
        db.update_draft_content(draft_id, approval_status="approved")
        db.create_schedule(draft_id, slot.isoformat())
        return RedirectResponse(url="/", status_code=303)

    @app.post("/drafts/{draft_id}/reject")
    def action_reject(draft_id: int) -> RedirectResponse:
        draft = db.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        db.update_draft_content(draft_id, approval_status="rejected")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/actions/ig-browser-login")
    async def action_ig_browser_login() -> RedirectResponse:
        automator = InstagramBrowserAutomator(cfg)
        await automator.launch_login_window(wait_seconds=90)
        return RedirectResponse(url="/", status_code=303)

    async def _run_publish(draft_id: int, mode: str | None = None) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        try:
            await pipeline.publish_draft(draft_id, mode=mode)
        except DuplicatePublishError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except MetaAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return RedirectResponse(url="/", status_code=303)

    @app.post("/drafts/{draft_id}/ig-browser")
    async def action_publish_ig_browser(
        draft_id: int,
        auto_share: str = Form("0"),
    ) -> RedirectResponse:
        mode = "browser_share" if auto_share == "1" else "browser"
        return await _run_publish(draft_id, mode=mode)

    @app.post("/drafts/{draft_id}/publish")
    async def action_publish(draft_id: int) -> RedirectResponse:
        return await _run_publish(draft_id)

    @app.post("/insights/record")
    def action_record_insight(
        publish_log_id: int = Form(...),
        reach: int = Form(1200),
        plays: int = Form(1600),
        likes: int = Form(95),
        comments: int = Form(14),
        saves: int = Form(62),
        shares: int = Form(28),
    ) -> RedirectResponse:
        pipeline = SmaiPipeline(cfg, db)
        snap = InsightSnapshot(
            horizon_hours=24,
            reach=reach,
            plays=plays,
            likes=likes,
            comments=comments,
            saves=saves,
            shares=shares,
        )
        pipeline.analyst.record_snapshot(publish_log_id, snap)
        return RedirectResponse(url="/", status_code=303)

    return app
