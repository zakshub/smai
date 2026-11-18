"""Procedural stereo ambient synth-pad and lo-fi pulse background music generator."""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path


CHORD_PROGRESSIONS: list[list[float]] = [
    [174.61, 220.00, 261.63, 329.63],  # Fmaj7 (F3, A3, C4, E4)
    [164.81, 196.00, 246.94, 293.66],  # Em7   (E3, G3, B3, D4)
    [146.83, 174.61, 220.00, 261.63],  # Dm7   (D3, F3, A3, C4)
    [130.81, 164.81, 196.00, 246.94],  # Cmaj7 (C3, E3, G3, B3)
]


def generate_ambient_bgm(
    out_path: Path,
    duration_sec: float = 18.0,
    sample_rate: int = 16000,
) -> Path:
    """
    Synthesize a warm stereo ambient lo-fi pad + subtle rhythmic pulse WAV track
    100% locally for mixing underneath Reel and Slide-Video voiceovers.
    Pre-renders each 4-second chord block once and tiles for instant speed.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total_sec = max(duration_sec, 2.0)
    chord_len_sec = 4.0
    chord_frames = int(chord_len_sec * sample_rate)
    two_pi = 2.0 * math.pi

    # Pre-render the 4 chord blocks (4s each = 16s full progression)
    chord_blocks: list[bytes] = []
    for chord in CHORD_PROGRESSIONS:
        block = bytearray(chord_frames * 4)
        sub_freq = chord[0] * 0.5
        f0, f1, f2, f3 = chord[0], chord[1], chord[2], chord[3]
        for i in range(chord_frames):
            local_t = i / sample_rate
            env = min(local_t / 0.4, 1.0) * min((chord_len_sec - local_t) / 0.5, 1.0)
            pulse = 0.72 + 0.28 * math.sin(two_pi * 1.5 * local_t)
            l_wave = (
                math.sin(two_pi * f0 * local_t)
                + math.sin(two_pi * f1 * local_t)
                + math.sin(two_pi * f2 * local_t)
                + math.sin(two_pi * f3 * local_t)
            )
            r_wave = (
                math.sin(two_pi * (f0 * 0.998) * local_t + 0.35)
                + math.sin(two_pi * (f1 * 1.001) * local_t + 0.35)
                + math.sin(two_pi * (f2 * 0.999) * local_t + 0.35)
                + math.sin(two_pi * (f3 * 1.002) * local_t + 0.35)
            )
            sub = 0.45 * math.sin(two_pi * sub_freq * local_t)
            scale = 1900.0 * env * pulse
            l_samp = int(max(-32000, min(32000, (l_wave * 0.25 + sub) * scale)))
            r_samp = int(max(-32000, min(32000, (r_wave * 0.25 + sub) * scale)))
            struct.pack_into("<hh", block, i * 4, l_samp, r_samp)
        chord_blocks.append(bytes(block))

    target_bytes = int(total_sec * sample_rate) * 4
    out_buf = bytearray()
    c_idx = 0
    while len(out_buf) < target_bytes:
        out_buf.extend(chord_blocks[c_idx % len(chord_blocks)])
        c_idx += 1

    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(out_buf[:target_bytes]))

    return out_path
