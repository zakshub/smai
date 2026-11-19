"""Unit tests for Meta Instagram Graph API Phase A (Auth, Account Discovery, Profile & Redaction)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient
import httpx

from smai.agents.account_analyzer import InstagramAccountAnalyzer
from smai.config import Settings
from smai.db.database import get_database
from smai.publishing.instagram_api import InstagramGraphClient, MetaAPIError, redact_secret
from smai.web.app import create_app


FAKE_TOKEN = "FAKE_SYSTEM_USER_TOKEN_1234567890"
VERIFIED_IG_ID = "17841401903161411"


def _meta_settings(tmp_path: Path, token: str = FAKE_TOKEN, ig_id: str = VERIFIED_IG_ID) -> Settings:
    return Settings(
        smai_data_dir=tmp_path / "data",
        smai_export_dir=tmp_path / "exports",
        smai_llm_provider="mock",
        smai_tts_provider="silent",
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

    # Verify Bearer auth header is used and token is never leaked in URLs or serialized health output
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
    client = InstagramGraphClient(settings, transport=httpx.MockTransport(error_handler))

    try:
        asyncio.run(client.validate_token())
        assert False, "Expected MetaAPIError"
    except MetaAPIError as exc:
        assert exc.status_code == 401
        assert exc.code == 190
        assert exc.error_subcode == 463
        assert exc.fbtrace_id == "TRACE_XYZ"
        assert FAKE_TOKEN not in str(exc)
        assert "FAKE...7890" in str(exc)

    assert FAKE_TOKEN not in redact_secret(f"url?access_token={FAKE_TOKEN}", FAKE_TOKEN)


def test_account_analyzer_uses_graph_api_client_and_normalizes_fields(tmp_path: Path):
    seen: list[dict[str, str]] = []
    settings = _meta_settings(tmp_path, ig_id="")  # omit ID to verify auto-discovery via assigned accounts
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
    settings = _meta_settings(tmp_path, token="", ig_id="")
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
