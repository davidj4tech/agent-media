"""sink-speech playback backend.

Adapter that conforms the watcher's PlaybackBackend interface to the
clean ``agent_media_core.sinks.SinkSpeech`` IPC client.  The actual mpv
broker is owned by the ``sink-speech`` runit service (openal, XDG
socket) — this backend never spawns mpv.

Replaces the legacy ``mpv`` + ``mpv-tts`` / ``mpv-voice`` brokers as the
default speech sink as of the Phase 2 cut-over.
"""

from __future__ import annotations

import time
from pathlib import Path

from agent_media_core.sinks import SinkSpeech
from agent_media_core.types import Target

from .base import PlaybackBackend


class SinkSpeechBackend(PlaybackBackend):
    name = "sink-speech"

    def __init__(self, target: str | None = None) -> None:
        self.target = Target(name=target or "local")
        self._sink = SinkSpeech()

    def wait_for_playback(self) -> None:
        # Poll mpv until idle. Cap at 120s — matches the mpv backend's
        # previous behaviour and protects the watcher from a stuck broker.
        for _ in range(1200):
            if self._sink.idle(self.target):
                return
            time.sleep(0.1)

    def play(self, path: Path) -> bool:
        try:
            self._sink.play(str(path), self.target)
        except Exception:  # noqa: BLE001 — keep the watcher alive on ipc errors
            return False
        # Block until this clip finishes so the queue stays sequential.
        # Give mpv a moment to flip out of idle before we start polling.
        for _ in range(20):
            if not self._sink.idle(self.target):
                break
            time.sleep(0.05)
        self.wait_for_playback()
        return True

    def describe(self) -> str:
        return f"sink-speech (target={self.target.name})"
