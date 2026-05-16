"""sink-music: thin MPD client for Mopidy.

Talks to Mopidy's MPD frontend (default port 6600). The actual content
type (music / audiobook / podcast / etc.) is tracked in `state/` and
drives interruption strategy in `route/` — this sink just plays.
"""

from __future__ import annotations

import os
import socket
from contextlib import contextmanager
from typing import Iterator, Optional

from ..types import Target


DEFAULT_TARGET = Target(name="local")


def _endpoint(target: Target) -> tuple[str, int]:
    if target.name != "local":
        raise NotImplementedError(f"sink-music target {target.name!r} not yet supported")
    host = os.environ.get("MEDIA_MPD_HOST") or os.environ.get("MPD_HOST", "127.0.0.1")
    port = int(os.environ.get("MEDIA_MPD_PORT") or os.environ.get("MPD_PORT", "6600"))
    return host, port


@contextmanager
def _connect(target: Target, timeout: float = 5.0) -> Iterator[socket.socket]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(_endpoint(target))
        # Read MPD greeting (`OK MPD 0.x\n`)
        s.recv(64)
        yield s
    finally:
        s.close()


def _cmd(s: socket.socket, line: str) -> str:
    s.sendall((line + "\n").encode())
    buf = b""
    while b"OK\n" not in buf and not buf.endswith(b"OK") and b"ACK" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
        if buf.endswith(b"OK\n") or b"\nACK" in buf or buf.startswith(b"ACK"):
            break
    return buf.decode(errors="replace")


class SinkMusic:
    """Sink protocol implementation for Mopidy / MPD."""

    def play(self, uri: str, target: Target = DEFAULT_TARGET,
             replace: bool = True, **_: object) -> None:
        with _connect(target) as s:
            if replace:
                _cmd(s, "clear")
            _cmd(s, f'add "{uri}"')
            _cmd(s, "play")

    def pause(self, target: Target = DEFAULT_TARGET) -> None:
        with _connect(target) as s:
            _cmd(s, "pause 1")

    def resume(self, target: Target = DEFAULT_TARGET) -> None:
        with _connect(target) as s:
            _cmd(s, "pause 0")

    def stop(self, target: Target = DEFAULT_TARGET) -> None:
        with _connect(target) as s:
            _cmd(s, "stop")

    def duck(self, target: Target = DEFAULT_TARGET, level: int = 15) -> None:
        with _connect(target) as s:
            _cmd(s, f"setvol {max(0, min(100, level))}")

    def unduck(self, target: Target = DEFAULT_TARGET, restore: int = 100) -> None:
        with _connect(target) as s:
            _cmd(s, f"setvol {restore}")

    def position(self, target: Target = DEFAULT_TARGET) -> Optional[int]:
        """Current playback position in ms, or None when not playing."""
        with _connect(target) as s:
            status = _cmd(s, "status")
        for line in status.splitlines():
            if line.startswith("elapsed:"):
                try:
                    return int(float(line.split(":", 1)[1].strip()) * 1000)
                except ValueError:
                    return None
        return None

    def seek_cur(self, target: Target = DEFAULT_TARGET,
                 position_ms: int = 0) -> None:
        """Seek the *current* track to an absolute position in ms.

        Used by the coordinator's pause-and-resume path to back up by
        the lead-in window so the listener doesn't miss the word that
        was playing when speech interrupted.
        """
        secs = max(0.0, position_ms / 1000.0)
        with _connect(target) as s:
            _cmd(s, f"seekcur {secs:.3f}")

    def now_playing_uri(self, target: Target = DEFAULT_TARGET) -> Optional[str]:
        with _connect(target) as s:
            current = _cmd(s, "currentsong")
        for line in current.splitlines():
            if line.startswith("file:"):
                return line.split(":", 1)[1].strip()
        return None
