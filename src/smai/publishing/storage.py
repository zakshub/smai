"""Public media staging helper for Meta Instagram Graph API."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

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
