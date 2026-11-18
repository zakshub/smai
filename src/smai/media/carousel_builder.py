"""Carousel, Single-Pic, Text-Post, and Story PNG renderer via Playwright Chromium with rich Pillow fallback."""

from __future__ import annotations

import base64
import hashlib
import os
import textwrap
from pathlib import Path
from typing import Any

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from smai.config import BrandBrain
from smai.db.models import CarouselSlide, ContentPackage, ReelScene
from smai.media.stock import generate_backdrop_image


def _template_env() -> Environment:
    tpl_dir = Path(__file__).resolve().parents[1] / "templates" / "carousel"
    return Environment(
        loader=FileSystemLoader(str(tpl_dir)),
        autoescape=select_autoescape(["html"]),
    )


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"]
        if bold
        else ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf"]
    )
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def _render_slide_pillow(
    brand: BrandBrain,
    slide: CarouselSlide,
    total: int,
    out_path: Path,
    width: int = 1080,
    height: int = 1350,
) -> Path:
    """Rich multi-layer glassmorphic fallback renderer when headless browser is disabled."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    is_light = brand.visual_style.carousel_theme == "clean-light"

    img = Image.new("RGBA", (width, height), (248, 250, 252, 255) if is_light else (8, 13, 26, 255))
    draw = ImageDraw.Draw(img)

    if not is_light:
        for y in range(height):
            r = y / height
            draw.line([(0, y), (width, y)], fill=(int(10 + 14 * (1 - r)), int(16 + 22 * r), int(34 + 38 * r), 255))
        glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(glow)
        gdraw.ellipse([(width - 420, -120), (width + 120, 420)], fill=(56, 189, 248, 85))
        gdraw.ellipse([(-140, height - 460), (380, height + 80)], fill=(99, 102, 241, 75))
        img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(radius=55)))

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)

    fg = (15, 23, 42, 255) if is_light else (248, 250, 252, 255)
    muted = (100, 116, 139, 255) if is_light else (148, 163, 184, 255)
    accent = (56, 189, 248, 255)

    font_tag = _load_font(26, bold=True)
    font_head = _load_font(60, bold=True)
    font_body = _load_font(34, bold=False)

    # Progress bar
    seg_w = (width - 160 - (total - 1) * 10) // max(total, 1)
    for i in range(1, total + 1):
        sx = 80 + (i - 1) * (seg_w + 10)
        col = accent if i <= slide.index else (148, 163, 184, 65)
        odraw.rounded_rectangle([(sx, 62), (sx + seg_w, 72)], radius=5, fill=col)

    # Top eyebrow + counter
    odraw.rounded_rectangle([(80, 96), (520, 154)], radius=29, outline=accent, width=3)
    odraw.text((106, 110), (slide.eyebrow or brand.name)[:24], fill=accent, font=font_tag)
    odraw.text((width - 190, 110), f"{slide.index:02d} / {total:02d}", fill=muted, font=font_tag)

    # Glassmorphic card
    card_bg = (255, 255, 255, 230) if is_light else (15, 23, 42, 210)
    odraw.rounded_rectangle(
        [(72, 200), (width - 72, height - 180)],
        radius=34,
        fill=card_bg,
        outline=(148, 163, 184, 70),
        width=2,
    )

    # Mini infographic bar chart strip inside card header
    chart_top = 240
    odraw.rounded_rectangle(
        [(112, chart_top), (width - 112, chart_top + 160)],
        radius=20,
        fill=(2, 6, 23, 150) if not is_light else (241, 245, 249, 255),
    )
    bar_heights = [46, 74, 108, 92, 124]
    for b_idx, bh in enumerate(bar_heights):
        bx = 160 + b_idx * 60
        by = chart_top + 138 - bh
        bcol = accent if b_idx == (slide.index % len(bar_heights)) else (99, 102, 241, 200)
        odraw.rounded_rectangle([(bx, by), (bx + 36, chart_top + 138)], radius=8, fill=bcol)

    if slide.accent_stat:
        odraw.rounded_rectangle([(width - 420, chart_top + 46), (width - 148, chart_top + 114)], radius=14, fill=accent)
        odraw.text((width - 396, chart_top + 64), slide.accent_stat[:18], fill=(2, 6, 23, 255), font=font_tag)

    y = chart_top + 200
    for line in textwrap.wrap(slide.headline, width=22)[:3]:
        odraw.text((116, y), line, fill=fg, font=font_head)
        y += 74

    y += 18
    for line in textwrap.wrap(slide.body, width=38)[:3]:
        odraw.text((116, y), line, fill=muted, font=font_body)
        y += 50

    if slide.bullets:
        y += 18
        for b_idx, b_text in enumerate(slide.bullets[:3], start=1):
            odraw.rounded_rectangle(
                [(116, y), (width - 116, y + 64)],
                radius=14,
                fill=(2, 6, 23, 140) if not is_light else (226, 232, 240, 200),
                outline=(56, 189, 248, 80),
                width=1,
            )
            odraw.text((140, y + 14), f"{b_idx}. {b_text[:40]}", fill=fg, font=font_body)
            y += 78

    odraw.text((80, height - 125), brand.visual_style.watermark, fill=muted, font=font_tag)
    cta_hint = "SWIPE ->" if slide.index < total else "SAVE & SHARE"
    odraw.text((width - 270, height - 125), cta_hint, fill=accent, font=font_tag)

    Image.alpha_composite(img, overlay).convert("RGB").save(out_path, format="PNG")
    return out_path


class CarouselBuilder:
    """Renders Carousel slides (4:5), Single Pics (4:5), Text Posts (4:5), or Stories (9:16) to PNG."""

    async def build(
        self,
        brand: BrandBrain,
        package: ContentPackage,
        output_dir: Path,
        prefer_playwright: bool = True,
    ) -> list[dict[str, Any]]:
        output_dir.mkdir(parents=True, exist_ok=True)
        if package.format == "story" and package.story_frames:
            slides = [
                CarouselSlide(
                    index=f.index,
                    role="value",
                    eyebrow=f.interactive_sticker or "STORY",
                    headline=f.headline,
                    body=f.body,
                )
                for f in package.story_frames
            ]
            width, height = 1080, 1920
        else:
            slides = package.slides
            width, height = 1080, 1350

        if not slides:
            return []

        env = _template_env()
        template = env.get_template("slide.html")
        total = len(slides)

        assets: list[dict[str, Any]] = []
        if prefer_playwright:
            try:
                assets = await self._render_with_playwright(
                    brand, package, slides, total, template, output_dir, width, height
                )
            except Exception:
                assets = []

        if not assets:
            for slide in slides:
                out_png = output_dir / f"slide_{slide.index:02d}.png"
                _render_slide_pillow(brand, slide, total, out_png, width=width, height=height)
                assets.append(
                    {
                        "asset_type": "slide_png",
                        "slide_index": slide.index,
                        "local_path": str(out_png),
                        "width": width,
                        "height": height,
                        "duration_sec": 0.0,
                    }
                )

        if package.format == "text_post":
            txt_path = output_dir / "post_text.txt"
            txt_path.write_text(package.text_body or package.caption, encoding="utf-8")
            assets.append(
                {
                    "asset_type": "text_post",
                    "slide_index": 99,
                    "local_path": str(txt_path),
                    "width": 0,
                    "height": 0,
                    "duration_sec": 0.0,
                }
            )
        return assets

    async def _render_with_playwright(
        self,
        brand: BrandBrain,
        package: ContentPackage,
        slides: list[CarouselSlide],
        total: int,
        template: Any,
        output_dir: Path,
        width: int,
        height: int,
    ) -> list[dict[str, Any]]:
        from playwright.async_api import async_playwright

        def _discover_executable() -> str | None:
            local_app = os.environ.get("LOCALAPPDATA", "")
            if local_app:
                pw_root = Path(local_app) / "ms-playwright"
                if pw_root.exists():
                    for pattern in (
                        "chromium-*/chrome-win64/chrome.exe",
                        "chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe",
                    ):
                        matches = sorted(pw_root.glob(pattern), reverse=True)
                        if matches:
                            return str(matches[0])
            for sys_browser in (
                "C:/Program Files/Google/Chrome/Application/chrome.exe",
                "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
            ):
                if Path(sys_browser).exists():
                    return sys_browser
            return None

        assets: list[dict[str, Any]] = []
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch()
            except Exception:
                exe = _discover_executable()
                if not exe:
                    raise
                browser = await p.chromium.launch(executable_path=exe)
            page = await browser.new_page(viewport={"width": width, "height": height})
            try:
                for slide in slides:
                    bg_path = output_dir / f"bg_{slide.index:02d}.png"
                    await self._prepare_slide_visual(brand, slide, bg_path)
                    bg_data_uri = ""
                    if bg_path.exists():
                        b64 = base64.b64encode(bg_path.read_bytes()).decode("ascii")
                        bg_data_uri = f"data:image/png;base64,{b64}"

                    html = template.render(
                        brand=brand,
                        slide=slide,
                        total=total,
                        theme=package.theme or brand.visual_style.carousel_theme,
                        width=width,
                        height=height,
                        bg_data_uri=bg_data_uri,
                    )
                    out_png = output_dir / f"slide_{slide.index:02d}.png"
                    await page.set_content(html, wait_until="load")
                    await page.screenshot(path=str(out_png), type="png")
                    assets.append(
                        {
                            "asset_type": "slide_png",
                            "slide_index": slide.index,
                            "local_path": str(out_png),
                            "width": width,
                            "height": height,
                            "duration_sec": 0.0,
                        }
                    )
            finally:
                await browser.close()
        return assets

    async def _prepare_slide_visual(
        self, brand: BrandBrain, slide: CarouselSlide, dest: Path
    ) -> Path:
        seed_slug = hashlib.md5(f"{slide.headline}_{slide.index}".encode("utf-8")).hexdigest()[:10]
        url = f"https://picsum.photos/seed/{seed_slug}/900/500"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=1.5), follow_redirects=True) as client:
                res = await client.get(url)
                res.raise_for_status()
                if len(res.content) > 4096:
                    dest.write_bytes(res.content)
                    return dest
        except Exception:
            pass
        return generate_backdrop_image(
            dest,
            brand,
            ReelScene(index=slide.index, visual_query=slide.headline, narration=slide.body),
            width=900,
            height=500,
        )
