"""Official Meta Instagram Graph API client for Reels, Carousels, Stories, and Insights."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from smai.config import Settings
from smai.db.models import InsightSnapshot


class InstagramGraphClient:
    """Async client for Meta Instagram Graph API (Business & Creator accounts)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.version = settings.ig_graph_api_version
        self.ig_user_id = settings.ig_user_id
        self.access_token = settings.ig_access_token
        self.base_url = f"https://graph.facebook.com/{self.version}"

    def _ensure_credentials(self) -> None:
        if not self.ig_user_id or not self.access_token:
            raise RuntimeError("IG_USER_ID and IG_ACCESS_TOKEN must be set for Graph API publishing.")

    async def publish_reel(self, video_url: str, caption: str) -> dict[str, str]:
        self._ensure_credentials()
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.post(
                f"{self.base_url}/{self.ig_user_id}/media",
                data={
                    "media_type": "REELS",
                    "video_url": video_url,
                    "caption": caption,
                    "share_to_feed": "true",
                    "access_token": self.access_token,
                },
            )
            res.raise_for_status()
            container_id = res.json()["id"]
            await self._wait_until_finished(client, container_id)
            pub_id = await self._publish_container(client, container_id)
            permalink = await self._fetch_permalink(client, pub_id)
            return {
                "ig_container_id": container_id,
                "ig_media_id": pub_id,
                "permalink": permalink,
            }

    async def publish_carousel(self, image_urls: list[str], caption: str) -> dict[str, str]:
        self._ensure_credentials()
        if len(image_urls) < 2:
            raise ValueError("Instagram Carousel requires at least 2 images.")
        async with httpx.AsyncClient(timeout=30.0) as client:
            child_ids: list[str] = []
            for img_url in image_urls[:10]:
                res = await client.post(
                    f"{self.base_url}/{self.ig_user_id}/media",
                    data={
                        "image_url": img_url,
                        "is_carousel_item": "true",
                        "access_token": self.access_token,
                    },
                )
                res.raise_for_status()
                child_ids.append(res.json()["id"])

            parent_res = await client.post(
                f"{self.base_url}/{self.ig_user_id}/media",
                data={
                    "media_type": "CAROUSEL",
                    "children": ",".join(child_ids),
                    "caption": caption,
                    "access_token": self.access_token,
                },
            )
            parent_res.raise_for_status()
            container_id = parent_res.json()["id"]
            await self._wait_until_finished(client, container_id)
            pub_id = await self._publish_container(client, container_id)
            permalink = await self._fetch_permalink(client, pub_id)
            return {
                "ig_container_id": container_id,
                "ig_media_id": pub_id,
                "permalink": permalink,
            }

    async def publish_image(self, image_url: str, caption: str) -> dict[str, str]:
        self._ensure_credentials()
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.post(
                f"{self.base_url}/{self.ig_user_id}/media",
                data={
                    "image_url": image_url,
                    "caption": caption,
                    "access_token": self.access_token,
                },
            )
            res.raise_for_status()
            container_id = res.json()["id"]
            await self._wait_until_finished(client, container_id)
            pub_id = await self._publish_container(client, container_id)
            permalink = await self._fetch_permalink(client, pub_id)
            return {
                "ig_container_id": container_id,
                "ig_media_id": pub_id,
                "permalink": permalink,
            }

    async def publish_story(self, image_url: str) -> dict[str, str]:
        self._ensure_credentials()
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.post(
                f"{self.base_url}/{self.ig_user_id}/media",
                data={
                    "media_type": "STORIES",
                    "image_url": image_url,
                    "access_token": self.access_token,
                },
            )
            res.raise_for_status()
            container_id = res.json()["id"]
            await self._wait_until_finished(client, container_id)
            pub_id = await self._publish_container(client, container_id)
            return {"ig_container_id": container_id, "ig_media_id": pub_id, "permalink": ""}

    async def fetch_insights(self, ig_media_id: str, horizon_hours: int = 24) -> InsightSnapshot:
        self._ensure_credentials()
        metrics = "reach,saved,shares,likes,comments,plays"
        async with httpx.AsyncClient(timeout=20.0) as client:
            res = await client.get(
                f"{self.base_url}/{ig_media_id}/insights",
                params={"metric": metrics, "access_token": self.access_token},
            )
            res.raise_for_status()
            data: list[dict[str, Any]] = res.json().get("data", [])

        parsed: dict[str, int] = {}
        for item in data:
            name = item.get("name", "")
            values = item.get("values", [])
            if values:
                parsed[name] = int(values[0].get("value", 0))

        return InsightSnapshot(
            horizon_hours=horizon_hours,
            reach=parsed.get("reach", 0),
            plays=parsed.get("plays", 0),
            likes=parsed.get("likes", 0),
            comments=parsed.get("comments", 0),
            saves=parsed.get("saved", 0),
            shares=parsed.get("shares", 0),
        )

    async def _wait_until_finished(
        self, client: httpx.AsyncClient, container_id: str, max_polls: int = 20
    ) -> None:
        for _ in range(max_polls):
            res = await client.get(
                f"{self.base_url}/{container_id}",
                params={"fields": "status_code", "access_token": self.access_token},
            )
            res.raise_for_status()
            status = res.json().get("status_code", "FINISHED")
            if status == "FINISHED":
                return
            if status == "ERROR":
                raise RuntimeError(f"Instagram media container {container_id} failed processing.")
            await asyncio.sleep(3.0)
        raise TimeoutError(f"Timed out waiting for container {container_id}.")

    async def _publish_container(self, client: httpx.AsyncClient, container_id: str) -> str:
        res = await client.post(
            f"{self.base_url}/{self.ig_user_id}/media_publish",
            data={"creation_id": container_id, "access_token": self.access_token},
        )
        res.raise_for_status()
        return str(res.json()["id"])

    async def _fetch_permalink(self, client: httpx.AsyncClient, ig_media_id: str) -> str:
        try:
            res = await client.get(
                f"{self.base_url}/{ig_media_id}",
                params={"fields": "permalink", "access_token": self.access_token},
            )
            res.raise_for_status()
            return str(res.json().get("permalink", ""))
        except Exception:
            return ""
