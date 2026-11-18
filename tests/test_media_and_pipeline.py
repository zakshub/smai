"""Integration tests for Carousel/Reel rendering, pipeline export, and FastAPI studio."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from smai.config import Settings, load_brand
from smai.db.database import get_database
from smai.db.models import ReelScene, WordTimestamp
from smai.media.bgm import generate_ambient_bgm
from smai.media.captions import build_ass_subtitles
from smai.media.reel_builder import build_final_mux_command, build_scene_clip_command
from smai.media.stock import VisualAssetItem
from smai.pipeline import SmaiPipeline
from smai.web.app import create_app


def _temp_settings(tmp_path: Path) -> Settings:
    return Settings(
        smai_data_dir=tmp_path / "data",
        smai_export_dir=tmp_path / "exports",
        smai_llm_provider="mock",
        smai_tts_provider="silent",
        smai_publish_mode="export",
        smai_enable_network_scout=False,
        smai_prefer_playwright=False,
    )


def test_ass_subtitle_and_ffmpeg_commands(tmp_path: Path):
    stamps = [
        WordTimestamp(word="Stop", start_sec=0.0, end_sec=0.3),
        WordTimestamp(word="wasting", start_sec=0.3, end_sec=0.7),
        WordTimestamp(word="time", start_sec=0.7, end_sec=1.1),
        WordTimestamp(word="today", start_sec=1.1, end_sec=1.5),
    ]
    ass_file = build_ass_subtitles(stamps, tmp_path / "captions.ass", words_per_chunk=3)
    content = ass_file.read_text(encoding="utf-8")
    assert "Dialogue:" in content
    assert "STOP WASTING" in content
    assert "\\fscx118\\fscy118" in content

    bgm_file = generate_ambient_bgm(tmp_path / "bgm.wav", duration_sec=3.0)
    assert bgm_file.exists() and bgm_file.stat().st_size > 10000

    img_visual = VisualAssetItem(path=tmp_path / "scene.png", kind="image", duration_sec=4.0)
    clip_cmd = build_scene_clip_command("ffmpeg", img_visual, 4.0, tmp_path / "clip.mp4")
    assert "zoompan=" in " ".join(clip_cmd)

    mux_cmd = build_final_mux_command(
        "ffmpeg",
        tmp_path / "concat.txt",
        tmp_path / "voice.wav",
        ass_file,
        tmp_path / "reel.mp4",
        12.0,
        bgm_path=bgm_file,
    )
    assert "amix=inputs=2" in " ".join(mux_cmd)


def test_end_to_end_carousel_and_reel_pipeline(tmp_path: Path):
    settings = _temp_settings(tmp_path)
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)

    # Generate & render carousel (Pillow fallback guarantees fast headless pass)
    carousel_res = asyncio.run(
        pipeline.generate_draft(
            fmt="carousel",
            custom_topic="3 automated habits for solo founders",
            render_media=True,
            prefer_playwright=False,
        )
    )
    draft_id = carousel_res["draft"]["id"]
    assert carousel_res["qa"]["status"] == "pass"
    assert len(carousel_res["assets"]) >= 5
    first_png = Path(carousel_res["assets"][0]["local_path"])
    assert first_png.exists()
    with Image.open(first_png) as im:
        assert im.size == (1080, 1350)

    # Export bundle
    pub_res = asyncio.run(pipeline.publish_draft(draft_id, mode="export"))
    export_dir = Path(pub_res["export_path"])
    assert (export_dir / "caption.txt").exists()
    assert (export_dir / "metadata.json").exists()
    assert (export_dir / first_png.name).exists()

    # Generate Reel (dry_run_reel=True verifies TTS WAV + ASS + backdrops without requiring FFmpeg in unit test)
    reel_res = asyncio.run(
        pipeline.generate_draft(
            fmt="reel",
            custom_topic="Why systems beat willpower",
            render_media=True,
            dry_run_reel=True,
        )
    )
    assert reel_res["draft"]["format"] == "reel"
    assert Path(reel_res["assets"][0]["local_path"]).exists()


def test_full_5_step_draft_cycle_all_formats(tmp_path: Path):
    settings = _temp_settings(tmp_path)
    db = get_database(settings)
    pipeline = SmaiPipeline(settings, db)

    results = asyncio.run(pipeline.run_full_draft_cycle(dry_run_reel=True))
    formats_created = [r["draft"]["format"] for r in results]
    assert formats_created == ["reel", "slide_video", "carousel", "single_pic", "text_post", "story"]

    for item in results:
        draft = item["draft"]
        assert draft["approval_status"] == "pending"
        assert draft["target_audience"]
        assert draft["recommended_time"]
        assert draft["algorithm_rationale"]
        assert item["qa"]["status"] == "pass"
        assert len(item["assets"]) >= 1

    text_draft = next(r for r in results if r["draft"]["format"] == "text_post")
    asset_paths = [Path(a["local_path"]) for a in text_draft["assets"]]
    assert any(p.name == "post_text.txt" and p.exists() for p in asset_paths)
    assert any(p.suffix == ".png" and p.exists() for p in asset_paths)


def test_fastapi_dashboard_and_approval_routes(tmp_path: Path):
    settings = _temp_settings(tmp_path)
    app = create_app(settings=settings, enable_scheduler=False)

    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        scout_resp = client.post("/actions/scout", follow_redirects=False)
        assert scout_resp.status_code == 303

        gen_resp = client.post(
            "/actions/generate",
            data={"fmt": "carousel", "custom_topic": "Daily focus checklist"},
            follow_redirects=False,
        )
        assert gen_resp.status_code == 303

        drafts = client.get("/api/drafts").json()
        assert len(drafts) == 1
        draft_id = drafts[0]["id"]

        approve_resp = client.post(f"/drafts/{draft_id}/approve", follow_redirects=False)
        assert approve_resp.status_code == 303

        pub_resp = client.post(f"/drafts/{draft_id}/publish", follow_redirects=False)
        assert pub_resp.status_code == 303

        ins_resp = client.post(
            "/insights/record",
            data={"publish_log_id": 1, "reach": 1000, "saves": 50, "shares": 25},
            follow_redirects=False,
        )
        assert ins_resp.status_code == 303

        home = client.get("/")
        assert home.status_code == 200
        assert "Daily focus checklist" in home.text
