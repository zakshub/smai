"""Public media staging helper for Meta Instagram Graph API."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from smai.config import Settings


class MediaStagingService:
    """Resolves or stages local media files to HTTPS URLs required by IG Graph API."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def resolve_public_url(self, local_path: Path) -> str:
        """
        Map a local rendered file inside data/media to a configured public HTTPS base URL
        (e.g., Cloudflare Tunnel, R2 custom domain, or reverse proxy).
        """
        base = self.settings.smai_public_media_base_url.strip().rstrip("/")
        if not base:
            raise RuntimeError(
                "SMAI_PUBLIC_MEDIA_BASE_URL is not configured. "
                "Instagram Graph API requires publicly accessible HTTPS media URLs, "
                "or switch SMAI_PUBLISH_MODE=export for local bundles."
            )
        media_root = self.settings.media_dir().resolve()
        resolved = local_path.resolve()
        try:
            rel = resolved.relative_to(media_root).as_posix()
        except ValueError:
            rel = resolved.name
        return f"{base}/{quote(rel)}"

    async def verify_public_url_reachable(
        self,
        url: str,
        timeout_sec: float = 10.0,
        expected_prefix: str | None = None,
    ) -> dict[str, Any]:
        """
        Verify a staged media URL uses HTTPS and serves HTTP 200/206 with an image/* or video/* Content-Type.
        Uses HEAD first and falls back to a bounded Range GET on HTTP 405/501.
        """
        cleaned = (url or "").strip()
        if not cleaned.lower().startswith("https://"):
            return {
                "ok": False,
                "url": cleaned,
                "status_code": None,
                "content_type": "",
                "method": "PRECHECK",
                "reason": "Public media URL must use https:// for Meta Instagram Graph API fetching.",
            }

        allowed_prefixes = (expected_prefix,) if expected_prefix else ("image/", "video/")
        try:
            async with httpx.AsyncClient(timeout=timeout_sec, follow_redirects=True) as client:
                method = "HEAD"
                resp = await client.head(cleaned)
                if resp.status_code in {405, 501}:
                    method = "GET"
                    async with client.stream(
                        "GET", cleaned, headers={"Range": "bytes=0-1023"}
                    ) as stream_resp:
                        status_code = stream_resp.status_code
                        content_type = stream_resp.headers.get("content-type", "").split(";")[0].strip().lower()
                else:
                    status_code = resp.status_code
                    content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "url": cleaned,
                "status_code": None,
                "content_type": "",
                "method": "HEAD",
                "reason": f"Public media URL is not reachable: {exc}",
            }

        if status_code not in {200, 206}:
            return {
                "ok": False,
                "url": cleaned,
                "status_code": status_code,
                "content_type": content_type,
                "method": method,
                "reason": f"Public media URL returned HTTP {status_code} (expected 200 or 206).",
            }

        if not any(content_type.startswith(prefix) for prefix in allowed_prefixes):
            return {
                "ok": False,
                "url": cleaned,
                "status_code": status_code,
                "content_type": content_type,
                "method": method,
                "reason": (
                    f"Public media URL returned unexpected Content-Type '{content_type or 'missing'}' "
                    f"(expected {', '.join(allowed_prefixes)})."
                ),
            }

        return {
            "ok": True,
            "url": cleaned,
            "status_code": status_code,
            "content_type": content_type,
            "method": method,
            "reason": "Public HTTPS media URL verified reachable.",
        }
