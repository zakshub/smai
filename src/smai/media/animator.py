"""Cinematic visual & motion-graphics video animator (radar nodes, animated charts, data packets, camera pan/zoom)."""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from smai.config import BrandBrain
from smai.db.models import ReelScene


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/calibrib.ttf"]
        if bold
        else ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/calibri.ttf"]
    )
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def _hex_to_rgb(hex_str: str, default: tuple[int, int, int] = (56, 189, 248)) -> tuple[int, int, int]:
    clean = hex_str.strip().lstrip("#")
    if len(clean) == 6:
        try:
            return int(clean[0:2], 16), int(clean[2:4], 16), int(clean[4:6], 16)
        except ValueError:
            return default
    return default


def _ease_out_cubic(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return 1.0 - pow(1.0 - x, 3)


def _draw_animated_visual_gizmo(
    draw: ImageDraw.ImageDraw,
    scene_index: int,
    t: float,
    progress: float,
    width: int,
    accent: tuple[int, int, int],
    font_lg: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    font_sm: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    """
    Draw true animated visual graphics (rotating orbital radar, moving node circuit packets,
    growing bar/line chart, or animated circular reactor gauge) over the cinematic scene.
    """
    cx, cy = width // 2, 680
    mode = (scene_index - 1) % 4

    # Translucent circular HUD plate behind the animated visual so it pops over any photo
    draw.ellipse([(cx - 310, cy - 310), (cx + 310, cy + 310)], fill=(8, 13, 26), outline=(56, 189, 248), width=3)
    draw.ellipse([(cx - 285, cy - 285), (cx + 285, cy + 285)], outline=(30, 41, 59), width=2)

    if mode == 0:
        # 1. Animated Orbital Radar & Node Network
        for r in (95, 175, 245):
            draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], outline=(30, 58, 95), width=2)
        sweep_deg = (t * 135.0) % 360.0
        draw.pieslice(
            [(cx - 245, cy - 245), (cx + 245, cy + 245)],
            start=sweep_deg - 42,
            end=sweep_deg,
            fill=(14, 116, 144),
        )
        # Orbiting satellite nodes
        pts: list[tuple[int, int]] = []
        for idx, radius in enumerate((95, 175, 245)):
            ang = t * (1.8 - idx * 0.45) + idx * 2.1
            px = int(cx + radius * math.cos(ang))
            py = int(cy + radius * math.sin(ang))
            pts.append((px, py))
        for i in range(len(pts) - 1):
            draw.line([pts[i], pts[i + 1]], fill=accent, width=3)
        for px, py in pts:
            draw.ellipse([(px - 16, py - 16), (px + 16, py + 16)], fill=accent)
            draw.ellipse([(px - 7, py - 7), (px + 7, py + 7)], fill=(248, 250, 252))
        pulse_r = int(28 + 10 * math.sin(t * 6.0))
        draw.ellipse([(cx - pulse_r, cy - pulse_r), (cx + pulse_r, cy + pulse_r)], fill=accent)

    elif mode == 1:
        # 2. Animated Circuit Pipeline with Traveling Laser Data Packets
        nodes = [(cx - 190, cy - 110), (cx, cy + 120), (cx + 190, cy - 110)]
        for i in range(len(nodes) - 1):
            draw.line([nodes[i], nodes[i + 1]], fill=(51, 65, 85), width=8)
        draw.line([nodes[0], nodes[2]], fill=(51, 65, 85), width=5)

        # Moving laser packet along the triangle path
        seg_pos = (t * 1.4) % 3.0
        seg_i = int(seg_pos)
        seg_frac = seg_pos - seg_i
        p_start = nodes[seg_i]
        p_end = nodes[(seg_i + 1) % 3]
        lx = int(p_start[0] + (p_end[0] - p_start[0]) * seg_frac)
        ly = int(p_start[1] + (p_end[1] - p_start[1]) * seg_frac)
        draw.ellipse([(lx - 22, ly - 22), (lx + 22, ly + 22)], fill=(34, 197, 94))
        draw.ellipse([(lx - 10, ly - 10), (lx + 10, ly + 10)], fill=(255, 255, 255))

        labels = ["INPUT", "ENGINE", "OUTPUT"]
        for idx, (nx, ny) in enumerate(nodes):
            active = idx == seg_i
            col = accent if active else (30, 41, 59)
            draw.rounded_rectangle([(nx - 68, ny - 44), (nx + 68, ny + 44)], radius=18, fill=col, outline=accent, width=3)
            txt_col = (2, 6, 23) if active else (248, 250, 252)
            draw.text((nx - 46, ny - 16), labels[idx], fill=txt_col, font=font_sm)

    elif mode == 2:
        # 3. Animated Growing Bar & Trajectory Chart + Live Counter
        grow = _ease_out_cubic(min(progress * 1.35, 1.0))
        targets = [70, 115, 155, 210, 275]
        base_y = cy + 160
        curve_pts: list[tuple[int, int]] = []
        for idx, target_h in enumerate(targets):
            bx = cx - 185 + idx * 82
            cur_h = max(int(target_h * grow), 12)
            by = base_y - cur_h
            bcol = (34, 197, 94) if idx == len(targets) - 1 else accent
            draw.rounded_rectangle([(bx, by), (bx + 48, base_y)], radius=10, fill=bcol)
            curve_pts.append((bx + 24, by - 16))
        if len(curve_pts) > 1:
            draw.line(curve_pts, fill=(248, 250, 252), width=5)
            last_x, last_y = curve_pts[-1]
            draw.ellipse([(last_x - 10, last_y - 10), (last_x + 10, last_y + 10)], fill=(34, 197, 94))
        pct = int(320 * grow)
        draw.text((cx - 95, cy - 235), f"+{pct}%", fill=(34, 197, 94), font=font_lg)

    else:
        # 4. Animated 360° Reactor Gauge & Radial Audio Spectrum Spokes
        sweep = int(320 * _ease_out_cubic(min(progress * 1.25, 1.0)))
        draw.arc([(cx - 210, cy - 210), (cx + 210, cy + 210)], start=-90, end=-90 + sweep, fill=accent, width=22)
        for spoke in range(24):
            ang = (spoke / 24.0) * 2.0 * math.pi + t * 0.8
            r_in = 115
            r_out = int(135 + 38 * abs(math.sin(t * 5.0 + spoke * 0.7)))
            x1, y1 = int(cx + r_in * math.cos(ang)), int(cy + r_in * math.sin(ang))
            x2, y2 = int(cx + r_out * math.cos(ang)), int(cy + r_out * math.sin(ang))
            draw.line([(x1, y1), (x2, y2)], fill=(99, 102, 241), width=4)
        val = int(100 * _ease_out_cubic(min(progress * 1.25, 1.0)))
        draw.text((cx - 72, cy - 36), f"{val}%", fill=(248, 250, 252), font=font_lg)


def render_animated_scene_clip(
    ffmpeg_bin: str,
    brand: BrandBrain,
    scene: ReelScene,
    total_scenes: int,
    duration_sec: float,
    out_clip: Path,
    backdrop_image: Path | None = None,
    fps: int = 16,
    width: int = 1080,
    height: int = 1920,
) -> Path:
    """
    Render a true visual & motion-graphics MP4 clip:
    - Real visual photograph / 3D scene backdrop with continuous camera pan & zoom
    - Live animated visual gizmo (orbital radar, circuit packets, growing chart, or reactor gauge)
    - Sleek lower-third title banner (no big text-slide box!)
    """
    out_clip.parent.mkdir(parents=True, exist_ok=True)
    dur = max(round(duration_sec, 2), 2.0)
    total_frames = max(int(dur * fps), fps * 2)

    primary_rgb = _hex_to_rgb(brand.visual_style.primary_color, (15, 23, 42))
    accent_rgb = _hex_to_rgb(brand.visual_style.accent_color, (56, 189, 248))

    # Pre-scale backdrop to 1240x2204 so camera pan/zoom is instant crop()
    overscan_w, overscan_h = 1220, 2168
    if backdrop_image and backdrop_image.exists():
        try:
            bg_overscan = Image.open(backdrop_image).convert("RGB").resize(
                (overscan_w, overscan_h), Image.Resampling.BILINEAR
            )
            dark_tint = Image.new("RGB", (overscan_w, overscan_h), (6, 10, 22))
            bg_overscan = Image.blend(bg_overscan, dark_tint, alpha=0.28)
        except Exception:
            bg_overscan = Image.new("RGB", (overscan_w, overscan_h), primary_rgb)
    else:
        bg_overscan = Image.new("RGB", (overscan_w, overscan_h), primary_rgb)

    font_tag = _load_font(28, bold=True)
    font_head = _load_font(46, bold=True)
    font_lg = _load_font(68, bold=True)

    title_str = (scene.on_screen_text or scene.narration[:36]).upper()[:32]
    seg_w = (width - 152 - (total_scenes - 1) * 12) // max(total_scenes, 1)

    cmd = [
        ffmpeg_bin,
        "-y",
        "-f",
        "rawvideo",
        "-vcodec",
        "rawvideo",
        "-s",
        f"{width}x{height}",
        "-pix_fmt",
        "rgb24",
        "-r",
        str(fps),
        "-i",
        "-",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "21",
        "-pix_fmt",
        "yuv420p",
        str(out_clip),
    ]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdin is not None

    max_dx = overscan_w - width
    max_dy = overscan_h - height

    try:
        for frame_idx in range(total_frames):
            t = frame_idx / fps
            progress = frame_idx / max(total_frames - 1, 1)

            # 1. Smooth diagonal camera pan across the high-res visual photograph
            if scene.index % 2 == 1:
                cx_off = int(max_dx * progress)
                cy_off = int(max_dy * progress)
            else:
                cx_off = int(max_dx * (1.0 - progress))
                cy_off = int(max_dy * progress)

            frame = bg_overscan.crop((cx_off, cy_off, cx_off + width, cy_off + height))
            draw = ImageDraw.Draw(frame)

            # 2. Top segmented progress bar & scene badge
            for s_idx in range(1, total_scenes + 1):
                sx = 76 + (s_idx - 1) * (seg_w + 12)
                draw.rounded_rectangle([(sx, 74), (sx + seg_w, 86)], radius=6, fill=(30, 41, 59))
                fill_w = seg_w if s_idx < scene.index else (int(seg_w * progress) if s_idx == scene.index else 0)
                if fill_w > 4:
                    draw.rounded_rectangle([(sx, 74), (sx + fill_w, 86)], radius=6, fill=accent_rgb)

            draw.rounded_rectangle(
                [(76, 112), (370, 168)],
                radius=28,
                fill=(10, 16, 31),
                outline=accent_rgb,
                width=3,
            )
            draw.text((104, 124), f"SCENE 0{scene.index} / 0{total_scenes}", fill=accent_rgb, font=font_tag)

            # 3. Animated visual graphics gizmo in center-upper viewport
            _draw_animated_visual_gizmo(
                draw=draw,
                scene_index=scene.index,
                t=t,
                progress=progress,
                width=width,
                accent=accent_rgb,
                font_lg=font_lg,
                font_sm=font_tag,
            )

            # 4. Compact lower-third banner (slides in smoothly; leaves room for pop captions below)
            slide_in = _ease_out_cubic(min(t / 0.35, 1.0))
            banner_x = int(76 - (1.0 - slide_in) * 120)
            draw.rounded_rectangle(
                [(banner_x, 1130), (width - 76, 1250)],
                radius=24,
                fill=(10, 16, 31),
                outline=accent_rgb,
                width=3,
            )
            draw.rounded_rectangle(
                [(banner_x + 20, 1152), (banner_x + 32, 1228)],
                radius=6,
                fill=accent_rgb,
            )
            draw.text((banner_x + 52, 1162), title_str, fill=(248, 250, 252), font=font_head)

            # 5. Live equalizer waveform & watermark footer
            eq_base_y = height - 140
            for bar_i in range(18):
                bar_x = 80 + bar_i * 22
                wave_h = int(10 + 24 * abs(math.sin(t * 6.5 + bar_i * 0.55)))
                draw.rounded_rectangle(
                    [(bar_x, eq_base_y - wave_h), (bar_x + 12, eq_base_y + wave_h)],
                    radius=5,
                    fill=accent_rgb,
                )
            draw.text((width - 390, eq_base_y - 16), brand.visual_style.watermark, fill=(248, 250, 252), font=font_tag)

            proc.stdin.write(frame.tobytes())
    finally:
        proc.stdin.close()
        proc.wait(timeout=60)

    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg rawvideo animation encode failed for scene {scene.index}")
    return out_clip
