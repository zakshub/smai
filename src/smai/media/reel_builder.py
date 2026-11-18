"""FFmpeg 9:16 Reel & Animated Slide-Video composer with motion graphics, BGM ducking, and ASS captions."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from smai.config import BrandBrain, Settings
from smai.db.models import ContentPackage, ReelScene
from smai.media.animator import render_animated_scene_clip
from smai.media.bgm import generate_ambient_bgm
from smai.media.captions import build_ass_subtitles
from smai.media.stock import StockMediaFetcher, VisualAssetItem
from smai.media.tts import TTSService


def resolve_ffmpeg_bin() -> str | None:
    """Locate ffmpeg on PATH, via cached Playwright ffmpeg, or via imageio-ffmpeg."""
    system_bin = shutil.which("ffmpeg")
    if system_bin:
        return system_bin
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        candidates = sorted(Path(local_app).glob("ms-playwright/ffmpeg-*/ffmpeg-win64.exe"), reverse=True)
        if candidates:
            return str(candidates[0])
    return None


def build_scene_clip_command(
    ffmpeg_bin: str,
    visual: VisualAssetItem,
    duration_sec: float,
    out_clip: Path,
) -> list[str]:
    """Build deterministic FFmpeg command to normalize a scene into 1080x1920 24fps MP4."""
    dur = max(round(duration_sec, 2), 1.5)
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,setsar=1,fps=24,format=yuv420p"
    )
    if visual.kind == "image":
        frames = int(dur * 24)
        vf = (
            f"scale=1200:2133:force_original_aspect_ratio=increase,"
            f"crop=1200:2133,"
            f"zoompan=z='min(zoom+0.0010,1.12)':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps=24,"
            f"format=yuv420p"
        )
        return [
            ffmpeg_bin,
            "-y",
            "-loop",
            "1",
            "-i",
            str(visual.path),
            "-t",
            str(dur),
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            str(out_clip),
        ]

    return [
        ffmpeg_bin,
        "-y",
        "-stream_loop",
        "-1",
        "-i",
        str(visual.path),
        "-t",
        str(dur),
        "-an",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        str(out_clip),
    ]


def build_final_mux_command(
    ffmpeg_bin: str,
    concat_file: Path,
    audio_path: Path,
    ass_path: Path,
    output_mp4: Path,
    total_duration: float,
    bgm_path: Path | None = None,
) -> list[str]:
    """
    Build FFmpeg command to mux concatenated video, voiceover + ducked ambient BGM,
    and burned-in ASS pop captions.
    """
    escaped_ass = str(ass_path).replace("\\", "/").replace(":", "\\:")
    if bgm_path and bgm_path.exists():
        return [
            ffmpeg_bin,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-i",
            str(audio_path),
            "-i",
            str(bgm_path),
            "-t",
            str(round(total_duration, 2)),
            "-filter_complex",
            (
                f"[0:v]ass='{escaped_ass}'[vout];"
                "[1:a]volume=1.0[vo];"
                "[2:a]volume=0.18[bgm];"
                "[vo][bgm]amix=inputs=2:duration=first:dropout_transition=2,"
                "loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
            ),
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_mp4),
        ]

    return [
        ffmpeg_bin,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-i",
        str(audio_path),
        "-t",
        str(round(total_duration, 2)),
        "-vf",
        f"ass='{escaped_ass}'",
        "-af",
        "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_mp4),
    ]


class ReelBuilder:
    """Orchestrates voiceover, ambient BGM, keyframed motion scenes, and FFmpeg assembly."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.tts = TTSService(settings)
        self.stock = StockMediaFetcher(settings)

    async def build(
        self,
        brand: BrandBrain,
        package: ContentPackage,
        output_dir: Path,
        dry_run: bool = False,
    ) -> dict[str, object]:
        output_dir.mkdir(parents=True, exist_ok=True)
        work_dir = output_dir / "work"
        work_dir.mkdir(parents=True, exist_ok=True)

        scenes: list[ReelScene] = list(package.scenes)
        if not scenes and package.slides:
            scenes = [
                ReelScene(
                    index=s.index,
                    visual_query=f"{brand.niche} {s.headline}",
                    narration=f"{s.headline}. {s.body}".strip(),
                    on_screen_text=s.headline,
                    duration_hint_sec=4.0,
                )
                for s in package.slides
            ]

        narration = package.narration_script or " ".join(s.narration for s in scenes)
        audio_path, timestamps, total_duration = await self.tts.synthesize(
            narration, work_dir, stem="voiceover"
        )
        bgm_dur = 2.0 if dry_run else (total_duration + 1.0)
        bgm_path = generate_ambient_bgm(work_dir / "bgm.wav", duration_sec=bgm_dur)
        ass_path = build_ass_subtitles(timestamps, work_dir / "captions.ass")
        visuals = await self.stock.fetch_scene_visuals(
            brand, scenes, work_dir, allow_network=not dry_run
        )

        ffmpeg_bin = resolve_ffmpeg_bin()
        output_mp4 = output_dir / "reel.mp4"

        if dry_run or not ffmpeg_bin:
            manifest = output_dir / "reel_manifest.txt"
            manifest.write_text(
                f"ffmpeg={ffmpeg_bin or 'missing'}\n"
                f"audio={audio_path}\n"
                f"bgm={bgm_path}\n"
                f"ass={ass_path}\n"
                f"duration={total_duration}\n"
                f"scenes={len(visuals)}\n",
                encoding="utf-8",
            )
            return {
                "asset_type": "video",
                "local_path": str(output_mp4 if output_mp4.exists() else manifest),
                "width": 1080,
                "height": 1920,
                "duration_sec": total_duration,
                "rendered_mp4": False,
            }

        raw_total = sum(v.duration_sec for v in visuals) or 1.0
        scale = total_duration / raw_total
        clip_paths: list[Path] = []
        total_scenes = len(scenes)

        for idx, (scene, visual) in enumerate(zip(scenes, visuals), start=1):
            clip_out = work_dir / f"clip_{idx:02d}.mp4"
            scene_dur = max(visual.duration_sec * scale, 2.0)

            if visual.kind == "video":
                cmd = build_scene_clip_command(
                    ffmpeg_bin=ffmpeg_bin,
                    visual=visual,
                    duration_sec=scene_dur,
                    out_clip=clip_out,
                )
                subprocess.run(cmd, check=True, capture_output=True)
            else:
                render_animated_scene_clip(
                    ffmpeg_bin=ffmpeg_bin,
                    brand=brand,
                    scene=scene,
                    total_scenes=total_scenes,
                    duration_sec=scene_dur,
                    out_clip=clip_out,
                    backdrop_image=visual.path,
                )
            clip_paths.append(clip_out)

        concat_txt = work_dir / "concat.txt"
        concat_txt.write_text(
            "\n".join(f"file '{p.resolve().as_posix()}'" for p in clip_paths) + "\n",
            encoding="utf-8",
        )

        mux_cmd = build_final_mux_command(
            ffmpeg_bin=ffmpeg_bin,
            concat_file=concat_txt,
            audio_path=audio_path,
            ass_path=ass_path,
            output_mp4=output_mp4,
            total_duration=total_duration,
            bgm_path=bgm_path,
        )
        subprocess.run(mux_cmd, check=True, capture_output=True)

        return {
            "asset_type": "video",
            "local_path": str(output_mp4),
            "width": 1080,
            "height": 1920,
            "duration_sec": total_duration,
            "rendered_mp4": True,
        }
