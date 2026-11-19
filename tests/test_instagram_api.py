"""Unit tests for Meta Instagram Graph API Phase A (Auth/Profile) & Phase B (Publishing/Reliability)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs

from fastapi.testclient import TestClient
import httpx

from smai.agents.account_analyzer import InstagramAccountAnalyzer
from smai.config import Settings
from smai.db.database import get_database
from smai.pipeline import SmaiPipeline
from smai.publishing.instagram_api import (
    DuplicatePublishError,
    InstagramGraphClient,
    MetaAPIError,
    redact_secret,
)
from smai.web.app import create_app


FAKE_TOKEN = "FAKE_SYSTEM_USER_TOKEN_1234567890"
VERIFIED_IG_ID = "17841401903161411"


def _meta_settings(
    tmp_path: Path,
    token: str = FAKE_TOKEN,
    ig_id: str = VERIFIED_IG_ID,
    publish_mode: str = "graph_api",
    public_base: str = "https://cdn.zuhaib.pro/smai",
) -> Settings:
    return Settings(
        smai_data_dir=tmp_path / "data",
        smai_export_dir=tmp_path / "exports",
        smai_llm_provider="mock",
        smai_tts_provider="silent",
        smai_publish_mode=publish_mode,
        smai_public_media_base_url=public_base,
        smai_enable_network_scout=False,
        smai_prefer_playwright=False,
        ig_user_id=ig_id,
        ig_access_token=token,
    )


def _make_phase_a_transport(seen_requests: list[dict[str, str]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "auth": request.headers.get("authorization", ""),
            }
        )
        path = request.url.path
        if path == "/v27.0/me":
            return httpx.Response(200, json={"id": "999888777", "name": "smai-system-user"})
        if path == "/v27.0/me/assigned_instagram_accounts":
            return httpx.Response(
                200,
                json={"data": [{"id": VERIFIED_IG_ID, "username": "zuhaibdotpro"}]},
            )
        if path == f"/v27.0/{VERIFIED_IG_ID}":
            return httpx.Response(
                200,
                json={
                    "id": VERIFIED_IG_ID,
                    "username": "zuhaibdotpro",
                    "name": "zaks",
                    "biography": "Pakistani drama commentary & storytelling",
                    "followers_count": 329,
                    "follows_count": 970,
                    "media_count": 199,
                    "profile_picture_url": "https://example.com/pic.jpg",
                },
            )
        if path == f"/v27.0/{VERIFIED_IG_ID}/media":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "18000111222",
                            "caption": "Episode 09 breakdown #greenentertainment #mittidebaway",
                            "media_type": "VIDEO",
                            "media_product_type": "REELS",
                            "timestamp": "2026-11-18T14:30:00+0000",
                            "like_count": 12,
                            "comments_count": 3,
                            "permalink": "https://www.instagram.com/reel/ABC123/",
                        }
                    ]
                },
            )
        return httpx.Response(
            404,
            json={"error": {"message": "Unknown path", "type": "OAuthException", "code": 803}},
        )

    return httpx.MockTransport(handler)


def test_settings_defaults_to_verified_v27_graph_api(tmp_path: Path):
    settings = _meta_settings(tmp_path)
    assert settings.ig_graph_api_version == "v27.0"
    client = InstagramGraphClient(settings)
    assert client.base_url == "https://graph.facebook.com/v27.0"


def test_phase_a_auth_discovery_profile_and_bearer_header(tmp_path: Path):
    seen: list[dict[str, str]] = []
    settings = _meta_settings(tmp_path)
    client = InstagramGraphClient(settings, transport=_make_phase_a_transport(seen))

    health = asyncio.run(client.check_connection_health())
    assert health["configured"] is True
    assert health["graph_api_version"] == "v27.0"
    assert health["graph_api_domain"] == "graph.facebook.com"
    assert health["token_valid"] is True
    assert health["token_identity"]["token_owner_name"] == "smai-system-user"
    assert health["assigned_accounts"] == [{"id": VERIFIED_IG_ID, "username": "zuhaibdotpro"}]
    assert health["active_account_verified"] is True
    assert health["profile"]["username"] == "zuhaibdotpro"
    assert health["profile"]["follows_count"] == 970
    assert health["errors"] == []

    assert len(seen) >= 3
    for req in seen:
        assert req["auth"] == f"Bearer {FAKE_TOKEN}"
        assert FAKE_TOKEN not in req["url"]
    assert FAKE_TOKEN not in json.dumps(health)


def test_meta_api_error_parses_structured_error_and_redacts_token(tmp_path: Path):
    def error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": {
                    "message": f"Invalid OAuth access token {FAKE_TOKEN}",
                    "type": "OAuthException",
                    "code": 190,
                    "error_subcode": 463,
                    "fbtrace_id": "TRACE_XYZ",
                }
            },
        )

    settings = _meta_settings(tmp_path)
    client = InstagramGraphClient(
        settings,
        transport=httpx.MockTransport(error_handler),
        retry_base_delay_sec=0.0,
    )

    try:
        asyncio.run(client.validate_token())
        assert False, "Expected MetaAPIError"
    except MetaAPIError as exc:
        assert exc.status_code == 401
        assert exc.code == 190
        assert exc.category == "auth"
        assert exc.is_transient is False
        assert exc.error_subcode == 463
        assert exc.fbtrace_id == "TRACE_XYZ"
        assert FAKE_TOKEN not in str(exc)
        assert "FAKE...7890" in str(exc)

    assert FAKE_TOKEN not in redact_secret(f"url?access_token={FAKE_TOKEN}", FAKE_TOKEN)


def test_account_analyzer_uses_graph_api_client_and_normalizes_fields(tmp_path: Path):
    seen: list[dict[str, str]] = []
    settings = _meta_settings(tmp_path, ig_id="")
    db = get_database(settings)
    analyzer = InstagramAccountAnalyzer(
        settings,
        db,
        graph_transport=_make_phase_a_transport(seen),
    )

    report = asyncio.run(analyzer.analyze(handle_or_url=""))
    assert report.data_source == "graph_api"
    assert report.handle == "@zuhaibdotpro"
    assert report.followers_count == 329
    assert report.following_count == 970
    assert report.media_count == 199
    assert report.recent_posts_analyzed == 1
    assert report.recent_posts[0].media_type == "REEL"
    assert "PKT" in report.recent_posts[0].posted_at


def test_web_meta_status_endpoint_never_exposes_token(tmp_path: Path):
    settings = _meta_settings(tmp_path, token="", ig_id="", publish_mode="export")
    app = create_app(settings=settings, enable_scheduler=False)

    with TestClient(app) as client:
        resp = client.get("/api/meta/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["configured"] is False
        assert body["graph_api_version"] == "v27.0"
        assert body["token_valid"] is False
        assert len(body["errors"]) == 1

        home = client.get("/")
        assert home.status_code == 200
        assert "Meta API (v27.0)" in home.text


# ----------------------------------------------------------------------
# Phase B: Content Publishing & Reliability Tests
# ----------------------------------------------------------------------


def test_phase_b_single_image_container_creation_polling_and_publish(tmp_path: Path):
    seen: list[dict[str, str]] = []
    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        body_text = request.content.decode("utf-8") if request.content else ""
        seen.append({"method": request.method, "path": request.url.path, "body": body_text})

        if request.method == "POST" and request.url.path == f"/v27.0/{VERIFIED_IG_ID}/media":
            form = parse_qs(body_text)
            assert form["image_url"] == ["https://cdn.zuhaib.pro/smai/draft_1/slide_01.png"]
            assert form["caption"] == ["Hello Instagram #smai"]
            return httpx.Response(200, json={"id": "CONT_1001"})

        if request.method == "GET" and request.url.path == "/v27.0/CONT_1001":
            poll_count += 1
            state = "IN_PROGRESS" if poll_count == 1 else "FINISHED"
            return httpx.Response(200, json={"status_code": state, "id": "CONT_1001"})

        if request.method == "POST" and request.url.path == f"/v27.0/{VERIFIED_IG_ID}/media_publish":
            form = parse_qs(body_text)
            assert form["creation_id"] == ["CONT_1001"]
            return httpx.Response(200, json={"id": "MEDIA_9001"})

        if request.method == "GET" and request.url.path == "/v27.0/MEDIA_9001":
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/p/XYZ9001/"})

        return httpx.Response(404, json={"error": {"message": "Unexpected call"}})

    settings = _meta_settings(tmp_path)
    client = InstagramGraphClient(
        settings,
        transport=httpx.MockTransport(handler),
        poll_interval_sec=0.0,
        retry_base_delay_sec=0.0,
    )

    container_id = asyncio.run(
        client.create_image_container(
            "https://cdn.zuhaib.pro/smai/draft_1/slide_01.png",
            caption="Hello Instagram #smai",
        )
    )
    assert container_id == "CONT_1001"

    poll_count = 0
    pub_res = asyncio.run(
        client.publish_image(
            "https://cdn.zuhaib.pro/smai/draft_1/slide_01.png",
            caption="Hello Instagram #smai",
        )
    )
    assert pub_res == {
        "ig_container_id": "CONT_1001",
        "ig_media_id": "MEDIA_9001",
        "permalink": "https://www.instagram.com/p/XYZ9001/",
        "status": "PUBLISHED",
    }
    assert poll_count == 2


def test_phase_b_container_published_state_does_not_republish(tmp_path: Path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "POST" and request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "CONT_ALREADY"})
        if request.method == "GET" and request.url.path == "/v27.0/CONT_ALREADY":
            return httpx.Response(200, json={"status_code": "PUBLISHED"})
        return httpx.Response(500, json={"error": {"message": "media_publish must not be called"}})

    client = InstagramGraphClient(
        _meta_settings(tmp_path),
        transport=httpx.MockTransport(handler),
        poll_interval_sec=0.0,
        retry_base_delay_sec=0.0,
    )
    res = asyncio.run(
        client.publish_image("https://cdn.zuhaib.pro/img.png", "Caption")
    )
    assert res["ig_container_id"] == "CONT_ALREADY"
    assert res["status"] == "PUBLISHED"
    assert not any("media_publish" in c for c in calls)


def test_phase_b_container_error_and_expired_raise_non_retryable_errors(tmp_path: Path):
    for bad_state in ("ERROR", "EXPIRED"):
        attempts = 0

        def handler(request: httpx.Request, state=bad_state) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if request.method == "POST" and request.url.path.endswith("/media"):
                return httpx.Response(200, json={"id": f"CONT_{state}"})
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "status_code": state,
                        "status": f"Media processing {state.lower()} ({FAKE_TOKEN})",
                    },
                )
            return httpx.Response(500, json={"error": {"message": "Must not reach media_publish"}})

        client = InstagramGraphClient(
            _meta_settings(tmp_path),
            transport=httpx.MockTransport(handler),
            poll_interval_sec=0.0,
            retry_base_delay_sec=0.0,
            max_retries=3,
        )
        try:
            asyncio.run(client.publish_image("https://cdn.zuhaib.pro/img.png", "Caption"))
            assert False, f"Expected MetaAPIError for container state {bad_state}"
        except MetaAPIError as exc:
            assert exc.container_status == bad_state
            assert exc.is_transient is False
            assert exc.category == "container_error"
            assert FAKE_TOKEN not in str(exc)
        # Exactly 1 POST /media + 1 GET /container_id (never retried or sent to media_publish)
        assert attempts == 2


def test_phase_b_transient_retry_and_non_retryable_stop_and_safe_publish_check(tmp_path: Path):
    # 1. Transient 503 on container creation retries and succeeds on 3rd try
    media_attempts = 0

    def retry_handler(request: httpx.Request) -> httpx.Response:
        nonlocal media_attempts
        if request.method == "POST" and request.url.path.endswith("/media"):
            media_attempts += 1
            if media_attempts < 3:
                return httpx.Response(
                    503,
                    json={"error": {"message": f"Temporary error {FAKE_TOKEN}", "code": 2, "is_transient": True}},
                )
            return httpx.Response(200, json={"id": "CONT_RETRY_OK"})
        if request.method == "GET" and request.url.path == "/v27.0/CONT_RETRY_OK":
            return httpx.Response(200, json={"status_code": "FINISHED"})
        if request.method == "POST" and request.url.path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "MEDIA_RETRY_OK"})
        return httpx.Response(200, json={"permalink": "https://www.instagram.com/p/OK/"})

    client = InstagramGraphClient(
        _meta_settings(tmp_path),
        transport=httpx.MockTransport(retry_handler),
        poll_interval_sec=0.0,
        retry_base_delay_sec=0.0,
        max_retries=3,
    )
    res = asyncio.run(client.publish_image("https://cdn.zuhaib.pro/ok.png", "Retry ok"))
    assert media_attempts == 3
    assert res["ig_media_id"] == "MEDIA_RETRY_OK"

    # 2. Non-recoverable 400 validation error (code=100) must NOT retry
    fatal_attempts = 0

    def fatal_handler(request: httpx.Request) -> httpx.Response:
        nonlocal fatal_attempts
        fatal_attempts += 1
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": f"Invalid image_url parameter token={FAKE_TOKEN}",
                    "type": "OAuthException",
                    "code": 100,
                }
            },
        )

    fatal_client = InstagramGraphClient(
        _meta_settings(tmp_path),
        transport=httpx.MockTransport(fatal_handler),
        poll_interval_sec=0.0,
        retry_base_delay_sec=0.0,
        max_retries=3,
    )
    try:
        asyncio.run(fatal_client.publish_image("https://cdn.zuhaib.pro/bad.png", "Bad"))
        assert False, "Expected non-retryable MetaAPIError"
    except MetaAPIError as exc:
        assert exc.is_transient is False
        assert exc.category == "validation"
        assert FAKE_TOKEN not in str(exc)
    assert fatal_attempts == 1

    # 3. Transient failure on media_publish checks container status first: if already PUBLISHED, does NOT re-POST
    pub_post_calls = 0

    def publish_timeout_handler(request: httpx.Request) -> httpx.Response:
        nonlocal pub_post_calls
        if request.method == "POST" and request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "CONT_CHECK"})
        if request.method == "GET" and request.url.path == "/v27.0/CONT_CHECK":
            # First poll: FINISHED; second check after timeout on media_publish: PUBLISHED
            state = "FINISHED" if pub_post_calls == 0 else "PUBLISHED"
            return httpx.Response(200, json={"status_code": state})
        if request.method == "POST" and request.url.path.endswith("/media_publish"):
            pub_post_calls += 1
            raise httpx.ReadTimeout("Timed out waiting for response")
        return httpx.Response(404, json={})

    safe_client = InstagramGraphClient(
        _meta_settings(tmp_path),
        transport=httpx.MockTransport(publish_timeout_handler),
        poll_interval_sec=0.0,
        retry_base_delay_sec=0.0,
        max_retries=3,
    )
    safe_res = asyncio.run(safe_client.publish_image("https://cdn.zuhaib.pro/t.png", "Safe"))
    assert pub_post_calls == 1
    assert safe_res["ig_container_id"] == "CONT_CHECK"
    assert safe_res["status"] == "PUBLISHED"


def test_phase_b_pipeline_duplicate_publishing_prevention_and_failure_recovery(tmp_path: Path):
    publish_calls = 0
    fail_first = True

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal publish_calls
        if request.method == "POST" and request.url.path.endswith("/media"):
            if fail_first:
                return httpx.Response(
                    400,
                    json={"error": {"message": f"Bad URL {FAKE_TOKEN}", "code": 100}},
                )
            return httpx.Response(200, json={"id": "CONT_PIPE_1"})
        if request.method == "GET" and request.url.path == "/v27.0/CONT_PIPE_1":
            return httpx.Response(200, json={"status_code": "FINISHED"})
        if request.method == "POST" and request.url.path.endswith("/media_publish"):
            publish_calls += 1
            return httpx.Response(200, json={"id": "MEDIA_PIPE_1"})
        if request.method == "GET" and request.url.path == "/v27.0/MEDIA_PIPE_1":
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/p/PIPE1/"})
        return httpx.Response(404, json={})

    settings = _meta_settings(tmp_path)
    db = get_database(settings)
    graph_client = InstagramGraphClient(
        settings,
        transport=httpx.MockTransport(handler),
        poll_interval_sec=0.0,
        retry_base_delay_sec=0.0,
    )
    pipeline = SmaiPipeline(settings, db, graph_client=graph_client)

    draft_res = asyncio.run(
        pipeline.generate_draft(
            fmt="single_pic",
            custom_topic="Automated systems cheatsheet",
            render_media=True,
            prefer_playwright=False,
        )
    )
    draft_id = int(draft_res["draft"]["id"])

    # 1. Pre-publish fatal failure must release the publish lock back to 'pending'
    try:
        asyncio.run(pipeline.publish_draft(draft_id, mode="graph_api"))
        assert False, "Expected MetaAPIError on first failed publish"
    except MetaAPIError as exc:
        assert FAKE_TOKEN not in str(exc)
    assert db.get_draft(draft_id)["approval_status"] == "pending"

    # 2. Retry after fixing upstream succeeds and records publish
    fail_first = False
    ok_res = asyncio.run(pipeline.publish_draft(draft_id, mode="graph_api"))
    assert ok_res["ig_media_id"] == "MEDIA_PIPE_1"
    assert ok_res["permalink"] == "https://www.instagram.com/p/PIPE1/"
    assert db.get_draft(draft_id)["approval_status"] == "published"
    assert publish_calls == 1

    # 3. Attempting to republish an already-published draft must be blocked
    try:
        asyncio.run(pipeline.publish_draft(draft_id, mode="graph_api"))
        assert False, "Expected DuplicatePublishError on second publish"
    except DuplicatePublishError:
        pass
    assert publish_calls == 1
    assert len(db.get_publish_logs_for_draft(draft_id)) == 1


def test_phase_b_web_publish_returns_409_on_duplicate(tmp_path: Path):
    settings = _meta_settings(tmp_path, token="", ig_id="", publish_mode="export")
    app = create_app(settings=settings, enable_scheduler=False)

    with TestClient(app) as client:
        client.post(
            "/actions/generate",
            data={"fmt": "single_pic", "custom_topic": "One-page checklist"},
            follow_redirects=False,
        )
        draft_id = client.get("/api/drafts").json()[0]["id"]

        first = client.post(f"/drafts/{draft_id}/publish", follow_redirects=False)
        assert first.status_code == 303

        second = client.post(f"/drafts/{draft_id}/publish", follow_redirects=False)
        assert second.status_code == 409
