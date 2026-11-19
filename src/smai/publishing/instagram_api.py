"""Official Meta Instagram Graph API client for authentication, publishing, insights, and engagement."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from smai.config import Settings
from smai.db.models import (
    AccountInsightResult,
    ApprovalRequiredError,
    DuplicatePublishError,
    InsightSnapshot,
    InvalidStateTransitionError,
)


TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
TRANSIENT_META_CODES = {1, 2, 4, 17, 32, 613}
NON_RETRYABLE_META_CODES = {10, 100, 102, 190, 200, 368, 9004}


def redact_secret(text: str, secret: str) -> str:
    """Ensure a secret token never appears in exception text, logs, or responses."""
    if not text or not secret:
        return text
    masked = f"{secret[:4]}...{secret[-4:]}" if len(secret) > 10 else "***"
    return text.replace(secret, masked)


class MetaAPIError(RuntimeError):
    """Structured error returned by the Meta Graph API or container processing pipeline."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        error_type: str = "",
        code: int | None = None,
        error_subcode: int | None = None,
        fbtrace_id: str = "",
        endpoint: str = "",
        is_transient: bool | None = None,
        container_status: str = "",
        error_user_title: str = "",
        error_user_msg: str = "",
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.error_type = error_type
        self.code = code
        self.error_subcode = error_subcode
        self.fbtrace_id = fbtrace_id
        self.endpoint = endpoint
        self.container_status = container_status
        self.error_user_title = error_user_title
        self.error_user_msg = error_user_msg

        if is_transient is not None:
            self.is_transient = is_transient
        else:
            self.is_transient = self._infer_transient()

        self.category = self._infer_category()

        parts = [f"Meta Graph API error ({status_code})"]
        if endpoint:
            parts.append(f"on {endpoint}")
        if container_status:
            parts.append(f"[container={container_status}]")
        if code is not None:
            parts.append(f"[code={code}, subcode={error_subcode}]")
        parts.append(message)
        if error_user_msg and error_user_msg not in message:
            parts.append(f"({error_user_msg})")
        super().__init__(" ".join(parts))

    def _infer_transient(self) -> bool:
        if self.container_status in ("ERROR", "EXPIRED", "PUBLISHED"):
            return False
        if self.code in NON_RETRYABLE_META_CODES:
            return False
        if self.code in TRANSIENT_META_CODES:
            return True
        if self.status_code in TRANSIENT_HTTP_STATUSES:
            return True
        if self.status_code == 0 and "HTTP transport failure" in self.message:
            return True
        return False

    def _infer_category(self) -> str:
        if self.container_status in ("ERROR", "EXPIRED", "PUBLISHED", "IN_PROGRESS"):
            return "container_error"
        if self.code in (102, 190) or self.status_code == 401:
            return "auth"
        if self.code in (10, 200) or self.status_code == 403:
            return "permission"
        if self.code in (4, 17, 32, 613) or self.status_code == 429:
            return "rate_limit"
        if self.is_transient:
            return "transient"
        if self.status_code == 400 or self.code in (100, 9004):
            return "validation"
        return "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status_code": self.status_code,
            "category": self.category,
            "is_transient": self.is_transient,
            "error_type": self.error_type,
            "code": self.code,
            "error_subcode": self.error_subcode,
            "fbtrace_id": self.fbtrace_id,
            "endpoint": self.endpoint,
            "container_status": self.container_status,
            "message": self.message,
        }


class InstagramGraphClient:
    """Async client for Meta Instagram Graph API (Business & Creator accounts)."""

    DOMAIN = "graph.facebook.com"
    PROFILE_FIELDS = (
        "id,username,name,biography,followers_count,follows_count,media_count,profile_picture_url"
    )
    MEDIA_FIELDS = (
        "id,caption,media_type,media_product_type,timestamp,like_count,comments_count,permalink"
    )

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_interval_sec: float = 3.0,
        max_polls: int = 20,
        max_retries: int = 3,
        retry_base_delay_sec: float = 1.0,
    ) -> None:
        self.settings = settings
        self.version = settings.ig_graph_api_version.strip() or "v27.0"
        self.ig_user_id = settings.ig_user_id.strip()
        self.access_token = settings.ig_access_token.strip()
        self.base_url = f"https://{self.DOMAIN}/{self.version}"
        self._transport = transport
        self.poll_interval_sec = max(0.0, poll_interval_sec)
        self.max_polls = max(1, max_polls)
        self.max_retries = max(1, max_retries)
        self.retry_base_delay_sec = max(0.0, retry_base_delay_sec)

    def _ensure_token(self) -> None:
        if not self.access_token:
            raise RuntimeError(
                "IG_ACCESS_TOKEN is not set. Configure IG_ACCESS_TOKEN in your local .env file."
            )

    def _ensure_credentials(self, ig_user_id: str | None = None) -> str:
        self._ensure_token()
        target_id = (ig_user_id or self.ig_user_id).strip()
        if not target_id:
            raise RuntimeError(
                "IG_USER_ID is not set. Configure IG_USER_ID in your local .env file "
                "or run account discovery via GET /me/assigned_instagram_accounts."
            )
        return target_id

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def _make_client(self, timeout_sec: float = 20.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout_sec, transport=self._transport)

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        clean_path = path.lstrip("/")
        url = f"{self.base_url}/{clean_path}"
        req_headers = headers if headers is not None else self._auth_headers()
        try:
            res = await client.request(
                method,
                url,
                params=params,
                data=data,
                headers=req_headers,
            )
        except httpx.HTTPError as exc:
            safe_msg = redact_secret(str(exc), self.access_token)
            raise MetaAPIError(
                message=f"HTTP transport failure: {safe_msg}",
                endpoint=clean_path,
                is_transient=True,
            ) from exc

        payload: dict[str, Any] = {}
        try:
            parsed = res.json()
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            payload = {}

        if res.status_code >= 400 or "error" in payload:
            err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            raw_msg = str(err.get("message") or res.text or f"HTTP {res.status_code}")
            explicit_transient = (
                bool(err["is_transient"]) if "is_transient" in err else None
            )
            raise MetaAPIError(
                message=redact_secret(raw_msg, self.access_token),
                status_code=res.status_code,
                error_type=str(err.get("type") or ""),
                code=int(err["code"]) if isinstance(err.get("code"), int) else None,
                error_subcode=(
                    int(err["error_subcode"]) if isinstance(err.get("error_subcode"), int) else None
                ),
                fbtrace_id=str(err.get("fbtrace_id") or ""),
                endpoint=clean_path,
                is_transient=explicit_transient,
                error_user_title=redact_secret(
                    str(err.get("error_user_title") or ""), self.access_token
                ),
                error_user_msg=redact_secret(
                    str(err.get("error_user_msg") or ""), self.access_token
                ),
            )

        return payload

    async def _request_with_retry(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Execute a Graph API request with exponential backoff for transient errors only."""
        last_exc: MetaAPIError | None = None
        for attempt in range(self.max_retries):
            try:
                return await self._request(
                    client, method, path, params=params, data=data, headers=headers
                )
            except MetaAPIError as exc:
                last_exc = exc
                if not exc.is_transient or attempt >= self.max_retries - 1:
                    raise
                delay = self.retry_base_delay_sec * (2**attempt)
                if delay > 0:
                    await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------------
    # Phase A: Authentication, Token Validation, Account Discovery & Profile
    # ------------------------------------------------------------------

    async def validate_token(self) -> dict[str, Any]:
        """Validate the configured System User / User token via GET /me and optional /debug_token."""
        self._ensure_token()
        async with self._make_client(15.0) as client:
            me_data = await self._request_with_retry(
                client, "GET", "me", params={"fields": "id,name"}
            )
            result: dict[str, Any] = {
                "valid": bool(me_data.get("id")),
                "token_owner_id": str(me_data.get("id") or ""),
                "token_owner_name": str(me_data.get("name") or ""),
                "graph_api_version": self.version,
            }
            app_id = self.settings.meta_app_id.strip()
            app_secret = self.settings.meta_app_secret.strip()
            if app_id and app_secret:
                try:
                    app_token = f"{app_id}|{app_secret}"
                    dbg = await self._request_with_retry(
                        client,
                        "GET",
                        "debug_token",
                        params={"input_token": self.access_token},
                        headers={"Authorization": f"Bearer {app_token}"},
                    )
                    dbg_data = dbg.get("data") if isinstance(dbg.get("data"), dict) else {}
                    result["debug_token"] = {
                        "is_valid": bool(dbg_data.get("is_valid", result["valid"])),
                        "app_id": str(dbg_data.get("app_id") or ""),
                        "type": str(dbg_data.get("type") or ""),
                        "expires_at": dbg_data.get("expires_at"),
                        "scopes": dbg_data.get("scopes") or [],
                    }
                except MetaAPIError as exc:
                    result["debug_token_error"] = exc.to_dict()
            return result

    async def discover_assigned_accounts(self) -> list[dict[str, str]]:
        """Discover Instagram accounts assigned to the System User token."""
        self._ensure_token()
        async with self._make_client(15.0) as client:
            payload = await self._request_with_retry(
                client,
                "GET",
                "me/assigned_instagram_accounts",
                params={"fields": "id,username"},
            )
        rows = payload.get("data") if isinstance(payload.get("data"), list) else []
        return [
            {"id": str(row.get("id") or ""), "username": str(row.get("username") or "")}
            for row in rows
            if isinstance(row, dict) and row.get("id")
        ]

    async def get_profile(self, ig_user_id: str | None = None) -> dict[str, Any]:
        """Retrieve verified Instagram profile fields for the configured or specified IG account ID."""
        target_id = self._ensure_credentials(ig_user_id)
        async with self._make_client(15.0) as client:
            return await self._request_with_retry(
                client,
                "GET",
                target_id,
                params={"fields": self.PROFILE_FIELDS},
            )

    async def list_media(self, ig_user_id: str | None = None, limit: int = 15) -> list[dict[str, Any]]:
        """List recent media objects on the Instagram Business/Creator account."""
        target_id = self._ensure_credentials(ig_user_id)
        async with self._make_client(20.0) as client:
            payload = await self._request_with_retry(
                client,
                "GET",
                f"{target_id}/media",
                params={"fields": self.MEDIA_FIELDS, "limit": limit},
            )
        rows = payload.get("data") if isinstance(payload.get("data"), list) else []
        return [row for row in rows if isinstance(row, dict)]

    async def check_connection_health(self, ig_user_id: str | None = None) -> dict[str, Any]:
        """Run non-destructive Phase A connection checks without exposing raw tokens."""
        target_id = (ig_user_id or self.ig_user_id).strip()
        report: dict[str, Any] = {
            "configured": bool(self.access_token),
            "configured_ig_user_id": target_id,
            "graph_api_version": self.version,
            "graph_api_domain": self.DOMAIN,
            "token_valid": False,
            "token_identity": {},
            "assigned_accounts": [],
            "active_account_verified": False,
            "profile": {},
            "errors": [],
        }
        if not self.access_token:
            report["errors"].append("IG_ACCESS_TOKEN is missing in environment / .env.")
            return report

        try:
            tok = await self.validate_token()
            report["token_valid"] = bool(tok.get("valid"))
            report["token_identity"] = tok
        except MetaAPIError as exc:
            report["errors"].append(exc.to_dict())
            return report

        try:
            assigned = await self.discover_assigned_accounts()
            report["assigned_accounts"] = assigned
            if not target_id and assigned:
                target_id = assigned[0]["id"]
                report["configured_ig_user_id"] = target_id
        except MetaAPIError as exc:
            report["errors"].append(exc.to_dict())

        if target_id:
            try:
                profile = await self.get_profile(target_id)
                report["profile"] = profile
                report["active_account_verified"] = bool(profile.get("id"))
            except MetaAPIError as exc:
                report["errors"].append(exc.to_dict())
        else:
            report["errors"].append("IG_USER_ID is missing and no assigned accounts were discovered.")

        return report

    # ------------------------------------------------------------------
    # Phase B: Content Publishing (Container Creation, Polling, Publish)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_media_url(media_url: str, label: str = "media_url") -> str:
        cleaned = media_url.strip()
        if not cleaned.startswith(("https://", "http://")):
            raise ValueError(f"{label} must be an absolute HTTP/HTTPS URL: {cleaned!r}")
        return cleaned

    async def create_image_container(
        self,
        image_url: str,
        caption: str = "",
        is_carousel_item: bool = False,
        ig_user_id: str | None = None,
    ) -> str:
        """Create an Instagram image media container via POST /{ig_user_id}/media."""
        target_id = self._ensure_credentials(ig_user_id)
        valid_url = self._validate_media_url(image_url, "image_url")
        data: dict[str, Any] = {"image_url": valid_url}
        if is_carousel_item:
            data["is_carousel_item"] = "true"
        elif caption:
            data["caption"] = caption
        async with self._make_client(30.0) as client:
            created = await self._request_with_retry(
                client, "POST", f"{target_id}/media", data=data
            )
        container_id = str(created.get("id") or "")
        if not container_id:
            raise MetaAPIError(
                message="Container creation succeeded without returning an id.",
                endpoint=f"{target_id}/media",
                is_transient=False,
            )
        return container_id

    async def get_container_status(self, container_id: str) -> dict[str, Any]:
        """Fetch status_code and status detail for a media container."""
        self._ensure_token()
        async with self._make_client(15.0) as client:
            return await self._request_with_retry(
                client,
                "GET",
                container_id,
                params={"fields": "status_code,status"},
            )

    async def publish_image(
        self, image_url: str, caption: str, ig_user_id: str | None = None
    ) -> dict[str, str]:
        """Create image container, poll status until FINISHED/PUBLISHED, and publish to IG."""
        target_id = self._ensure_credentials(ig_user_id)
        valid_url = self._validate_media_url(image_url, "image_url")
        async with self._make_client(30.0) as client:
            created = await self._request_with_retry(
                client,
                "POST",
                f"{target_id}/media",
                data={"image_url": valid_url, "caption": caption},
            )
            container_id = str(created.get("id") or "")
            if not container_id:
                raise MetaAPIError(
                    message="Container creation returned empty id.",
                    endpoint=f"{target_id}/media",
                    is_transient=False,
                )
            return await self._finalize_container_publish(client, target_id, container_id)

    async def publish_reel(
        self, video_url: str, caption: str, ig_user_id: str | None = None
    ) -> dict[str, str]:
        target_id = self._ensure_credentials(ig_user_id)
        valid_url = self._validate_media_url(video_url, "video_url")
        async with self._make_client(30.0) as client:
            created = await self._request_with_retry(
                client,
                "POST",
                f"{target_id}/media",
                data={
                    "media_type": "REELS",
                    "video_url": valid_url,
                    "caption": caption,
                    "share_to_feed": "true",
                },
            )
            container_id = str(created["id"])
            return await self._finalize_container_publish(client, target_id, container_id)

    async def publish_carousel(
        self, image_urls: list[str], caption: str, ig_user_id: str | None = None
    ) -> dict[str, str]:
        target_id = self._ensure_credentials(ig_user_id)
        if len(image_urls) < 2:
            raise ValueError("Instagram Carousel requires at least 2 images.")
        async with self._make_client(30.0) as client:
            child_ids: list[str] = []
            for img_url in image_urls[:10]:
                valid_url = self._validate_media_url(img_url, "image_url")
                child = await self._request_with_retry(
                    client,
                    "POST",
                    f"{target_id}/media",
                    data={"image_url": valid_url, "is_carousel_item": "true"},
                )
                child_ids.append(str(child["id"]))

            parent = await self._request_with_retry(
                client,
                "POST",
                f"{target_id}/media",
                data={
                    "media_type": "CAROUSEL",
                    "children": ",".join(child_ids),
                    "caption": caption,
                },
            )
            container_id = str(parent["id"])
            return await self._finalize_container_publish(client, target_id, container_id)

    async def publish_story(
        self, image_url: str, ig_user_id: str | None = None
    ) -> dict[str, str]:
        target_id = self._ensure_credentials(ig_user_id)
        valid_url = self._validate_media_url(image_url, "image_url")
        async with self._make_client(30.0) as client:
            created = await self._request_with_retry(
                client,
                "POST",
                f"{target_id}/media",
                data={"media_type": "STORIES", "image_url": valid_url},
            )
            container_id = str(created["id"])
            return await self._finalize_container_publish(
                client, target_id, container_id, fetch_permalink=False
            )

    async def _finalize_container_publish(
        self,
        client: httpx.AsyncClient,
        target_id: str,
        container_id: str,
        fetch_permalink: bool = True,
    ) -> dict[str, str]:
        final_state = await self._wait_until_finished(client, container_id)
        if final_state == "PUBLISHED":
            # Container is already published on Meta side — never call media_publish again
            return {
                "ig_container_id": container_id,
                "ig_media_id": "",
                "permalink": "",
                "status": "PUBLISHED",
            }
        pub_id = await self._publish_container_safe(client, target_id, container_id)
        permalink = await self._fetch_permalink(client, pub_id) if fetch_permalink else ""
        return {
            "ig_container_id": container_id,
            "ig_media_id": pub_id,
            "permalink": permalink,
            "status": "PUBLISHED",
        }

    async def _wait_until_finished(
        self, client: httpx.AsyncClient, container_id: str
    ) -> str:
        """
        Poll container status until FINISHED or PUBLISHED.
        Raises non-retryable MetaAPIError on ERROR or EXPIRED, and transient error on timeout.
        """
        for attempt in range(self.max_polls):
            payload = await self._request_with_retry(
                client,
                "GET",
                container_id,
                params={"fields": "status_code,status"},
            )
            status_code = str(payload.get("status_code") or "").upper()
            status_detail = redact_secret(str(payload.get("status") or ""), self.access_token)

            if status_code == "FINISHED":
                return "FINISHED"
            if status_code == "PUBLISHED":
                return "PUBLISHED"
            if status_code == "ERROR":
                detail = status_detail or "Instagram media container failed processing."
                raise MetaAPIError(
                    message=f"Container {container_id} failed: {detail}",
                    error_type="ContainerError",
                    endpoint=container_id,
                    is_transient=False,
                    container_status="ERROR",
                )
            if status_code == "EXPIRED":
                detail = status_detail or "Instagram media container expired before publishing."
                raise MetaAPIError(
                    message=f"Container {container_id} expired: {detail}",
                    error_type="ContainerError",
                    endpoint=container_id,
                    is_transient=False,
                    container_status="EXPIRED",
                )
            # IN_PROGRESS or empty transient status
            if attempt < self.max_polls - 1 and self.poll_interval_sec > 0:
                await asyncio.sleep(self.poll_interval_sec)

        raise MetaAPIError(
            message=f"Timed out waiting for container {container_id} to finish processing.",
            error_type="ContainerTimeout",
            endpoint=container_id,
            is_transient=True,
            container_status="IN_PROGRESS",
        )

    async def _publish_container_safe(
        self, client: httpx.AsyncClient, target_id: str, container_id: str
    ) -> str:
        """
        Publish a FINISHED container with transient retry protection.
        Before retrying a failed media_publish POST, checks container status so an
        already-succeeded publish is never retried blindly.
        """
        last_exc: MetaAPIError | None = None
        for attempt in range(self.max_retries):
            try:
                payload = await self._request(
                    client,
                    "POST",
                    f"{target_id}/media_publish",
                    data={"creation_id": container_id},
                )
                return str(payload["id"])
            except MetaAPIError as exc:
                last_exc = exc
                if not exc.is_transient or attempt >= self.max_retries - 1:
                    raise
                delay = self.retry_base_delay_sec * (2**attempt)
                if delay > 0:
                    await asyncio.sleep(delay)
                # Inspect container state before retrying POST media_publish
                try:
                    check = await self._request_with_retry(
                        client,
                        "GET",
                        container_id,
                        params={"fields": "status_code,status"},
                    )
                    state = str(check.get("status_code") or "").upper()
                    if state == "PUBLISHED":
                        return ""
                    if state in ("ERROR", "EXPIRED"):
                        raise MetaAPIError(
                            message=f"Container {container_id} transitioned to {state} during publish retry.",
                            error_type="ContainerError",
                            endpoint=container_id,
                            is_transient=False,
                            container_status=state,
                        )
                except MetaAPIError as check_exc:
                    if check_exc.container_status in ("ERROR", "EXPIRED"):
                        raise
        assert last_exc is not None
        raise last_exc

    async def _fetch_permalink(self, client: httpx.AsyncClient, ig_media_id: str) -> str:
        if not ig_media_id:
            return ""
        try:
            payload = await self._request_with_retry(
                client,
                "GET",
                ig_media_id,
                params={"fields": "permalink"},
            )
            return str(payload.get("permalink", ""))
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Performance Intelligence: v27.0 Insights (probed metric-by-metric)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_insight_value(item: dict[str, Any], prefer_sum: bool = False) -> int | None:
        """Extract an integer metric value from Meta v27.0 total_value or values payloads."""
        total_val = item.get("total_value")
        if isinstance(total_val, dict):
            raw = total_val.get("value")
            if isinstance(raw, (int, float)):
                return int(raw)
        values = item.get("values")
        if isinstance(values, list) and values:
            nums = [
                int(entry["value"])
                for entry in values
                if isinstance(entry, dict) and isinstance(entry.get("value"), (int, float))
            ]
            if nums:
                return sum(nums) if prefer_sum else nums[-1]
        return None

    async def fetch_account_insights_safe(
        self,
        ig_user_id: str | None = None,
        window_days: int = 1,
    ) -> AccountInsightResult:
        """
        Collect account-level Meta Graph API v27.0 insights safely by probing metrics
        individually with explicit metric_type handling.
        Unsupported metrics are recorded in unsupported_metrics without aborting or fabricating 0.
        """
        self._ensure_credentials()
        target_id = ig_user_id or self.settings.ig_user_id
        now = datetime.now(timezone.utc)
        since_ts = int((now - timedelta(days=max(1, window_days))).timestamp())
        until_ts = int(now.timestamp())

        metric_specs: list[tuple[str, str, tuple[str, ...]]] = [
            ("reach", "day", ("total_value", "time_series")),
            ("views", "day", ("total_value",)),
            ("profile_views", "day", ("total_value",)),
            ("accounts_engaged", "day", ("total_value",)),
            ("total_interactions", "day", ("total_value",)),
            ("follower_count", "day", ("time_series",)),
        ]

        parsed: dict[str, int] = {}
        raw_metrics: dict[str, int | float | str | None] = {}
        unsupported: list[str] = []
        metric_errors: dict[str, str] = {}

        async with self._make_client(20.0) as client:
            for metric_name, period, metric_types in metric_specs:
                collected = False
                last_err: MetaAPIError | None = None
                for mtype in metric_types:
                    try:
                        payload = await self._request_with_retry(
                            client,
                            "GET",
                            f"{target_id}/insights",
                            params={
                                "metric": metric_name,
                                "period": period,
                                "metric_type": mtype,
                                "since": since_ts,
                                "until": until_ts,
                            },
                        )
                        items: list[dict[str, Any]] = payload.get("data", [])
                        if items:
                            val = self._extract_insight_value(
                                items[0],
                                prefer_sum=(mtype == "time_series"),
                            )
                            if val is not None:
                                parsed[metric_name] = val
                                raw_metrics[f"{metric_name}:{mtype}"] = val
                        collected = True
                        break
                    except MetaAPIError as exc:
                        last_err = exc
                        # Auth/permission errors must surface immediately instead of being masked.
                        if exc.code in {10, 190, 200} or exc.status_code in {401, 403}:
                            raise
                        # Retry alternative metric_type only when Meta rejects metric_type syntax.
                        msg_l = exc.message.lower()
                        if "metric_type" in msg_l or "time_series" in msg_l or "total_value" in msg_l:
                            continue
                        break
                if not collected and last_err is not None:
                    unsupported.append(metric_name)
                    metric_errors[metric_name] = redact_secret(
                        f"code={last_err.code} status={last_err.status_code}: {last_err.message}",
                        self.settings.ig_access_token,
                    )

        return AccountInsightResult(
            ig_user_id=target_id,
            window_days=max(1, window_days),
            captured_at=now.isoformat(),
            source="meta_graph_api_v27",
            reach=parsed.get("reach"),
            views=parsed.get("views"),
            profile_views=parsed.get("profile_views"),
            accounts_engaged=parsed.get("accounts_engaged"),
            total_interactions=parsed.get("total_interactions"),
            follower_delta=parsed.get("follower_count"),
            raw_metrics=raw_metrics,
            unsupported_metrics=unsupported,
            metric_errors=metric_errors,
        )

    async def fetch_media_insights_safe(
        self,
        ig_media_id: str,
        media_product_type: str = "FEED",
        horizon_hours: int = 24,
    ) -> InsightSnapshot:
        """
        Collect media-level Meta Graph API v27.0 insights metric-by-metric so deprecated
        or surface-specific metrics fail gracefully into unsupported_metrics without fabricating 0s.
        """
        self._ensure_credentials()
        product_type = (media_product_type or "FEED").strip().upper()
        base_like_count: int | None = None
        base_comments_count: int | None = None
        raw_metrics: dict[str, int | float | str | None] = {}
        unsupported: list[str] = []
        parsed: dict[str, int] = {}

        async with self._make_client(20.0) as client:
            # Fetch base media fields first for reliable product_type & direct like/comment counts.
            try:
                media_obj = await self._request_with_retry(
                    client,
                    "GET",
                    ig_media_id,
                    params={"fields": "id,media_type,media_product_type,like_count,comments_count"},
                )
                if media_obj.get("media_product_type"):
                    product_type = str(media_obj["media_product_type"]).upper()
                if isinstance(media_obj.get("like_count"), (int, float)):
                    base_like_count = int(media_obj["like_count"])
                    raw_metrics["media_like_count"] = base_like_count
                if isinstance(media_obj.get("comments_count"), (int, float)):
                    base_comments_count = int(media_obj["comments_count"])
                    raw_metrics["media_comments_count"] = base_comments_count
            except MetaAPIError as exc:
                if exc.code in {10, 190, 200} or exc.status_code in {401, 403}:
                    raise

            if "REEL" in product_type:
                metrics = [
                    "reach",
                    "views",
                    "plays",
                    "saved",
                    "shares",
                    "likes",
                    "comments",
                    "total_interactions",
                    "ig_reels_avg_watch_time",
                ]
            elif "STORY" in product_type:
                metrics = ["reach", "views", "shares", "replies", "profile_visits", "follows"]
            else:
                metrics = [
                    "reach",
                    "views",
                    "saved",
                    "shares",
                    "likes",
                    "comments",
                    "profile_visits",
                    "follows",
                    "total_interactions",
                ]

            for metric_name in metrics:
                try:
                    payload = await self._request_with_retry(
                        client,
                        "GET",
                        f"{ig_media_id}/insights",
                        params={"metric": metric_name},
                    )
                except MetaAPIError as exc:
                    if exc.code in {10, 190, 200} or exc.status_code in {401, 403}:
                        raise
                    if "metric_type" in exc.message.lower():
                        try:
                            payload = await self._request_with_retry(
                                client,
                                "GET",
                                f"{ig_media_id}/insights",
                                params={"metric": metric_name, "metric_type": "total_value"},
                            )
                        except MetaAPIError as inner_exc:
                            unsupported.append(metric_name)
                            raw_metrics[f"error:{metric_name}"] = redact_secret(
                                inner_exc.message, self.settings.ig_access_token
                            )
                            continue
                    else:
                        unsupported.append(metric_name)
                        raw_metrics[f"error:{metric_name}"] = redact_secret(
                            exc.message, self.settings.ig_access_token
                        )
                        continue

                items: list[dict[str, Any]] = payload.get("data", [])
                if items:
                    val = self._extract_insight_value(items[0])
                    if val is not None:
                        parsed[metric_name] = val
                        raw_metrics[metric_name] = val

        return InsightSnapshot(
            horizon_hours=horizon_hours,
            reach=parsed.get("reach"),
            views=parsed.get("views"),
            plays=parsed.get("plays"),
            likes=parsed.get("likes") if "likes" in parsed else base_like_count,
            comments=parsed.get("comments") if "comments" in parsed else base_comments_count,
            saves=parsed.get("saved"),
            shares=parsed.get("shares"),
            profile_visits=parsed.get("profile_visits"),
            follows=parsed.get("follows"),
            avg_watch_time_ms=parsed.get("ig_reels_avg_watch_time"),
            source="meta_graph_api_v27",
            raw_metrics=raw_metrics,
            unsupported_metrics=unsupported,
        )

    async def fetch_insights(
        self,
        ig_media_id: str,
        horizon_hours: int = 24,
        media_product_type: str = "FEED",
    ) -> InsightSnapshot:
        return await self.fetch_media_insights_safe(
            ig_media_id=ig_media_id,
            media_product_type=media_product_type,
            horizon_hours=horizon_hours,
        )
