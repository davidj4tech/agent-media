"""Tiny WAV header writer for PCM streams (used by realtime engine)."""

from __future__ import annotations

import struct


def wav_wrap(pcm: bytes, sample_rate: int, sample_width: int, channels: int) -> bytes:
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm),
        b"WAVE",
        b"fmt ", 16, 1, channels, sample_rate,
        byte_rate, block_align, sample_width * 8,
        b"data", len(pcm),
    )
    return header + pcm
