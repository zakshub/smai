"""ASS subtitle generator with viral pop-in scale animations and word highlights."""

from __future__ import annotations

from pathlib import Path

from smai.db.models import WordTimestamp


ASS_HEADER = """[Script Info]
Title: SMAI Reel Captions
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Segoe UI,76,&H00FFFFFF,&H0038BDF8,&H00080C14,&H96000000,-1,0,0,0,100,100,1.5,0,1,6,3,2,70,70,360,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _format_ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis >= 100:
        secs += 1
        centis = 0
    return f"{hrs}:{mins:02d}:{secs:02d}.{centis:02d}"


def build_ass_subtitles(
    timestamps: list[WordTimestamp],
    out_path: Path,
    words_per_chunk: int = 3,
    accent_hex_bgr: str = "F8BD38",
) -> Path:
    """
    Group word timestamps into punchy 2-4 word viral subtitle events with
    pop-in scale transform (\\fscx118\\fscy118\\t(0,110,\\fscx100\\fscy100)) and active word highlight.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [ASS_HEADER]

    for i in range(0, len(timestamps), words_per_chunk):
        chunk = timestamps[i : i + words_per_chunk]
        if not chunk:
            continue
        start = chunk[0].start_sec
        end = max(chunk[-1].end_sec, start + 0.35)

        styled_words: list[str] = []
        for idx, item in enumerate(chunk):
            clean = item.word.replace("{", "").replace("}", "").upper()
            if idx == len(chunk) - 1:
                styled_words.append(f"{{\\c&H00{accent_hex_bgr}&}}{clean}{{\\c&H00FFFFFF&}}")
            else:
                styled_words.append(clean)

        pop_fx = "{\\fscx118\\fscy118\\t(0,110,\\fscx100\\fscy100)}"
        dialogue = pop_fx + " ".join(styled_words)
        lines.append(
            f"Dialogue: 0,{_format_ass_time(start)},{_format_ass_time(end)},Default,,0,0,0,,{dialogue}"
        )

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path
