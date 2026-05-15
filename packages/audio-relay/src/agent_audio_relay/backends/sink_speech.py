"""sink-speech playback backend.

Adapter that conforms the watcher's PlaybackBackend interface to the
clean ``agent_media_core.sinks.SinkSpeech`` IPC client. The actual mpv
broker is owned by the ``sink-speech`` runit service (openal, XDG
socket) — this backend never spawns mpv.

Phase 3: before each play, asks the route coordinator to apply
interruption (duck or pause-and-resume) to sink-music; after play,
restores. Each clip is also recorded in the state store's history
table.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from agent_media_core.route import Coordinator
from agent_media_core.sinks import SinkSpeech
from agent_media_core.state import StateStore
from agent_media_core.types import Target

from .base import PlaybackBackend


log = logging.getLogger(__name__)


class SinkSpeechBackend(PlaybackBackend):
    name = "sink-speech"

    def __init__(self, target: str | None = None) -> None:
        self.target = Target(name=target or "local")
        self._sink = SinkSpeech()
        self._state = StateStore()
        self._coordinator = Coordinator(state=self._state)

    def wait_for_playback(self) -> None:
        # Poll mpv until idle. Cap at 120s — matches the prior backend's
        # behaviour and keeps the watcher unblocked if the broker stalls.
        for _ in range(1200):
            if self._sink.idle(self.target):
                return
            time.sleep(0.1)

    def play(self, path: Path) -> bool:
        started_at = time.time()
        self._coordinator.before_speech()
        try:
            try:
                self._sink.play(str(path), self.target)
            except Exception as e:  # noqa: BLE001
                log.warning("sink-speech: play failed: %s", e)
                self._state.log_error("sink-speech", "play failed",
                                      extras={"path": str(path), "detail": str(e)})
                return False
            # Give mpv a moment to flip out of idle before we start
            # polling — otherwise the first idle read can race the
            # loadfile reply.
            for _ in range(20):
                if not self._sink.idle(self.target):
                    break
                time.sleep(0.05)
            self.wait_for_playback()
        finally:
            self._coordinator.after_speech()
            self._state.add_history(
                sink="speech",
                uri=str(path),
                started_at=started_at,
                ended_at=time.time(),
                target=self.target.name,
                source="watcher",
            )
        return True

    def describe(self) -> str:
        return f"sink-speech (target={self.target.name})"
