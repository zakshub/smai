"""Visual sourcing adapter: Pexels video, real high-res photography, Pollinations AI, and procedural scene art."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import httpx
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from smai.config import BrandBrain, Settings
from smai.db.models import ReelScene


class VisualAssetItem:
    def __init__(self, path: Path, kind: Literal["video", "image"], duration_sec: float) -> None:
        self.path = path
        self.kind = kind
        self.duration_sec = duration_sec


def generate_backdrop_image(
    out_path: Path,
    brand: BrandBrain,
    scene: ReelScene,
    width: int = 1080,
    height: int = 1920,
) -> Path:
    """
    Create a rich procedural 3D-style cyber/architectural visual artwork
    (isometric perspective grid, glowing horizon sun/reactor, floating data nodes,
    and bokeh light fields) when offline.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (width, height), (6, 10, 22, 255))
    draw = ImageDraw.Draw(img)

    # 1. Deep twilight sky gradient
    horizon_y = int(height * 0.56)
    for y in range(height):
        ratio = y / height
        r = int(8 + 28 * math.sin(ratio * math.pi))
        g = int(14 + 42 * ratio)
        b = int(32 + 78 * (1.0 - abs(ratio - 0.52)))
        draw.line([(0, y), (width, y)], fill=(r, g, b, 255))

    # 2. Glowing reactor / sun & atmospheric nebulae
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    seed = scene.index * 1.7
    sun_x = int(width * (0.35 + 0.3 * abs(math.sin(seed))))
    sun_y = int(horizon_y - 80)
    for rad, col in (
        (460, (56, 189, 248, 70)),
        (320, (99, 102, 241, 95)),
        (190, (14, 165, 233, 135)),
        (95, (248, 250, 252, 180)),
    ):
        gdraw.ellipse([(sun_x - rad, sun_y - rad), (sun_x + rad, sun_y + rad)], fill=col)
    glow = glow.filter(ImageFilter.GaussianBlur(radius=45))
    img = Image.alpha_composite(img, glow)

    # 3. 3D perspective horizon floor grid
    vdraw = ImageDraw.Draw(img)
    vanish_x = sun_x
    for angle_i in range(-14, 15):
        end_x = int(vanish_x + angle_i * 190)
        vdraw.line([(vanish_x, horizon_y), (end_x, height)], fill=(56, 189, 248, 75), width=2)
    for step_i in range(1, 16):
        t = pow(step_i / 15.0, 1.8)
        gy = int(horizon_y + t * (height - horizon_y))
        alpha = int(35 + 95 * t)
        vdraw.line([(0, gy), (width, gy)], fill=(56, 189, 248, alpha), width=2)

    # 4. Constellation network nodes in upper sky
    nodes: list[tuple[int, int]] = []
    for n_i in range(12):
        nx = int(110 + ((n_i * 233 + scene.index * 137) % (width - 220)))
        ny = int(140 + ((n_i * 179 + scene.index * 97) % (horizon_y - 200)))
        nodes.append((nx, ny))
    for i in range(len(nodes) - 1):
        vdraw.line([nodes[i], nodes[i + 1]], fill=(148, 163, 184, 65), width=2)
    for nx, ny in nodes:
        vdraw.ellipse([(nx - 10, ny - 10), (nx + 10, ny + 10)], fill=(56, 189, 248, 210))
        vdraw.ellipse([(nx - 4, ny - 4), (nx + 4, ny + 4)], fill=(248, 250, 252, 255))

    img.convert("RGB").save(out_path, format="PNG")
    return out_path


class StockMediaFetcher:
    """Fetches Pexels video, real high-res photography, AI illustrations, or 3D procedural art."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def fetch_scene_visuals(
        self,
        brand: BrandBrain,
        scenes: list[ReelScene],
        output_dir: Path,
        allow_network: bool = True,
    ) -> list[VisualAssetItem]:
        output_dir.mkdir(parents=True, exist_ok=True)
        items: list[VisualAssetItem] = []

        for scene in scenes:
            downloaded_video: Path | None = None
            if allow_network and self.settings.pexels_api_key:
                downloaded_video = await self._download_pexels_video(
                    query=scene.visual_query,
                    dest=output_dir / f"scene_{scene.index:02d}.mp4",
                )
            if downloaded_video is not None:
                items.append(
                    VisualAssetItem(
                        path=downloaded_video,
                        kind="video",
                        duration_sec=scene.duration_hint_sec,
                    )
                )
                continue

            photo_img: Path | None = None
            if allow_network and self.settings.smai_enable_network_scout:
                photo_img = await self._download_real_visual_photo(
                    query=scene.visual_query,
                    seed=scene.index,
                    dest=output_dir / f"scene_{scene.index:02d}.png",
                )

            if photo_img is None:
                photo_img = generate_backdrop_image(
                    out_path=output_dir / f"scene_{scene.index:02d}.png",
                    brand=brand,
                    scene=scene,
                )

            items.append(
                VisualAssetItem(
                    path=photo_img,
                    kind="image",
                    duration_sec=scene.duration_hint_sec,
                )
            )
        return items

    async def _download_real_visual_photo(
        self, query: str, seed: int, dest: Path, width: int = 1080, height: int = 1920
    ) -> Path | None:
        """
        Download a real high-resolution visual photograph (Picsum / Pollinations) and apply
        cinematic high-contrast color grading so scenes look like real film/photography.
        """
        seed_slug = hashlib.md5(f"{query}_{seed}".encode("utf-8")).hexdigest()[:10]
        urls = [
            f"https://picsum.photos/seed/{seed_slug}/{width}/{height}",
            f"https://image.pollinations.ai/prompt/{quote(query + ' cinematic photography 8k no text')}?width={width}&height={height}&seed={seed}&nologo=true",
        ]
        timeout = httpx.Timeout(4.0, connect=2.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            for url in urls:
                try:
                    res = await client.get(url)
                    res.raise_for_status()
                    if len(res.content) < 8192:
                        continue
                    dest.write_bytes(res.content)
                    # Verify & apply cinematic dark contrast boost with Pillow
                    with Image.open(dest) as im:
                        rgb = im.convert("RGB").resize((width, height), Image.Resampling.BILINEAR)
                        rgb = ImageEnhance.Contrast(rgb).enhance(1.22)
                        rgb = ImageEnhance.Color(rgb).enhance(1.15)
                        rgb.save(dest, format="PNG")
                    return dest
                except Exception:
                    continue
        return None

    async def _download_pexels_video(self, query: str, dest: Path) -> Path | None:
        url = "https://api.pexels.com/videos/search"
        headers = {"Authorization": self.settings.pexels_api_key}
        params = {"query": query, "orientation": "portrait", "per_page": 3}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.get(url, headers=headers, params=params)
                res.raise_for_status()
                videos = res.json().get("videos", [])
                if not videos:
                    return None
                files = videos[0].get("video_files", [])
                hd = next((f for f in files if f.get("width", 0) >= 720), files[0] if files else None)
                if not hd:
                    return None
                media_res = await client.get(hd["link"])
                media_res.raise_for_status()
                dest.write_bytes(media_res.content)
                return dest
        except Exception:
            return None
