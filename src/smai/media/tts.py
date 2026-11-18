"""Text-to-Speech adapters with word-level boundary timestamps."""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import httpx

from smai.config import Settings
from smai.db.models import WordTimestamp


def estimate_word_timestamps(text: str, total_duration_sec: float) -> list[WordTimestamp]:
    """Fallback proportional word timing when a TTS provider omits word boundaries."""
    words = [w for w in text.split() if w.strip()]
    if not words:
        return []
    weights = [max(len(w), 2) for w in words]
    total_weight = sum(weights)
    cursor = 0.0
    stamps: list[WordTimestamp] = []
    for word, weight in zip(words, weights):
        dur = (weight / total_weight) * max(total_duration_sec, 1.0)
        stamps.append(
            WordTimestamp(
                word=word,
                start_sec=round(cursor, 3),
                end_sec=round(cursor + dur, 3),
            )
        )
        cursor += dur
    return stamps


def write_silent_wav(out_path: Path, duration_sec: float = 8.0, sample_rate: int = 22050) -> Path:
    """Create a valid PCM WAV audio file for offline/dry-run Reel builds."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    num_frames = int(max(duration_sec, 1.0) * sample_rate)
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        # Subtle low-amplitude warm pulse so audio streams remain non-empty
        chunk = bytearray()
        for i in range(num_frames):
            sample = int(120 * math.sin(2 * math.pi * 110 * (i / sample_rate)))
            chunk.extend(struct.pack("<h", sample))
        wf.writeframes(bytes(chunk))
    return out_path


class TTSService:
    """Synthesizes faceless Reel voiceovers and returns audio path + word timestamps."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def synthesize(
        self, text: str, output_dir: Path, stem: str = "voiceover"
    ) -> tuple[Path, list[WordTimestamp], float]:
        output_dir.mkdir(parents=True, exist_ok=True)
        provider = self.settings.smai_tts_provider.lower().strip()

        if provider == "elevenlabs" and self.settings.elevenlabs_api_key:
            try:
                return await self._synthesize_elevenlabs(text, output_dir, stem)
            except Exception:
                pass

        if provider == "edge":
            try:
                return await self._synthesize_edge(text, output_dir, stem)
            except Exception:
                pass

        # Fallback silent/tone WAV for offline environments
        word_count = max(len(text.split()), 1)
        est_duration = max(round(word_count / 2.5, 2), 4.0)
        wav_path = write_silent_wav(output_dir / f"{stem}.wav", duration_sec=est_duration)
        stamps = estimate_word_timestamps(text, est_duration)
        return wav_path, stamps, est_duration

    async def _synthesize_edge(
        self, text: str, output_dir: Path, stem: str
    ) -> tuple[Path, list[WordTimestamp], float]:
        import edge_tts

        audio_path = output_dir / f"{stem}.mp3"
        communicate = edge_tts.Communicate(
            text=text,
            voice=self.settings.smai_tts_voice,
            boundary="WordBoundary",
        )
        stamps: list[WordTimestamp] = []
        audio_chunks = bytearray()
        max_end = 0.0

        async for chunk in communicate.stream():
            ctype = chunk.get("type")
            if ctype == "audio":
                audio_chunks.extend(chunk["data"])
            elif ctype == "WordBoundary":
                # edge-tts offsets and durations are in 100-nanosecond ticks
                start_sec = float(chunk["offset"]) / 10_000_000.0
                dur_sec = float(chunk["duration"]) / 10_000_000.0
                end_sec = start_sec + max(dur_sec, 0.08)
                max_end = max(max_end, end_sec)
                stamps.append(
                    WordTimestamp(
                        word=str(chunk["text"]),
                        start_sec=round(start_sec, 3),
                        end_sec=round(end_sec, 3),
                    )
                )

        if not audio_chunks:
            raise RuntimeError("edge-tts returned empty audio stream")

        audio_path.write_bytes(bytes(audio_chunks))
        duration_sec = max(max_end + 0.35, round(len(text.split()) / 2.5, 2), 3.0)
        if not stamps:
            stamps = estimate_word_timestamps(text, duration_sec)
        return audio_path, stamps, round(duration_sec, 2)

    async def _synthesize_elevenlabs(
        self, text: str, output_dir: Path, stem: str
    ) -> tuple[Path, list[WordTimestamp], float]:
        voice_id = self.settings.elevenlabs_voice_id or "21m00Tcm4TlvDq8ikWAM"
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        headers = {
            "xi-api-key": self.settings.elevenlabs_api_key,
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
        }
        payload = {
            "text": text,
            "model_id": "eleven_multilingual_v2",
        }
        async with httpx.AsyncClient(timeout=45.0) as client:
            res = await client.post(url, headers=headers, json=payload)
            res.raise_for_status()
        audio_path = output_dir / f"{stem}.mp3"
        audio_path.write_bytes(res.content)
        est_duration = max(round(len(text.split()) / 2.5, 2), 4.0)
        return audio_path, estimate_word_timestamps(text, est_duration), est_duration
