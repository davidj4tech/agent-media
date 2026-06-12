"""sink-book: longform (audiobook / podcast) player — the book channel.

A second mpv broker, distinct from sink-speech (TTS clips) and sink-music
(Mopidy). The book channel is its own long-running mpv on mel with
book-shaped transport: resume-by-URI bookmarks (held in the state store),
playback speed, skip ±N seconds, and its own PulseAudio/PipeWire stream
(client name `agent-media-book`) so a later phase can mix it under music.

Unlike sink-speech — whose broker is started by a service — this class
lazy-spawns its broker the first time the channel is used, so it works the
moment you call `book play`. Set MEDIA_BOOK_AUTOSPAWN=0 to require an
externally-managed broker instead.

Probe/control methods never spawn: with no broker running they treat the
channel as idle, so the speech coordinator can cheaply ask "is a book
playing?" without starting mpv.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from .._paths import state_dir
from ..types import Target
from . import _mpv_ipc as ipc


log = logging.getLogger(__name__)

DEFAULT_TARGET = Target(name="local")

# mpv playback-speed sane bounds.
_MIN_SPEED = 0.25
_MAX_SPEED = 4.0


def normalize_uri(uri: str) -> str:
    """Coerce a URI into something mpv understands — and the bookmark key.

    The agent/music side speaks Mopidy URIs (`yt:https://...`); mpv plays
    YouTube via yt-dlp from the bare URL. Strip a leading `yt:` / `youtube:`
    so the same URI a user hands to `music_play` also works here.
    Everything else (http(s), file://, local paths) passes through.
    """
    u = uri.strip()
    for prefix in ("yt:", "youtube:"):
        if u.startswith(prefix):
            return u[len(prefix):]
    return u


def _socket_path() -> Path:
    override = os.environ.get("MEDIA_BOOK_SOCKET")
    if override:
        return Path(override)
    return state_dir() / "sink-book.sock"


def _device_for(target: Target) -> Optional[str]:
    """mpv `audio-device` for a target. None = mpv's default device.

    - `local` → mpv's default device (mel's hardware out; override with
      MEDIA_BOOK_DEVICE) — at-desk listening, not through Snapcast.
    - `rooms` → the whole-house Snapcast feed the room snapclients actually
      play, which is the same sink speech uses (`MEDIA_ROOMS_SINK`, default
      `am`); override per-book-channel with MEDIA_BOOK_ROOMS_SINK. The book
      is mel-side mpv audio just like speech, so it has to ride the stream
      the rooms subscribe to. Music plays on p8ar locally (Mopidy), so
      "both at once" mixes at the p8ar device — its local music plus the
      `am` snapclient stream carrying the book — with the bed level being
      the two players' independent volumes.
    """
    if target.name == "local":
        override = os.environ.get("MEDIA_BOOK_DEVICE")
        if override and override.lower() not in ("", "auto", "default"):
            return override
        return None
    if target.name == "rooms":
        sink = (os.environ.get("MEDIA_BOOK_ROOMS_SINK")
                or os.environ.get("MEDIA_ROOMS_SINK") or "am")
        return f"pulse/{sink}"
    raise NotImplementedError(f"sink-book target {target.name!r} not configured")


class SinkBook:
    """The book channel: a longform mpv player with resume + speed + skip."""

    def __init__(self) -> None:
        self._sock = _socket_path()

    # ---- broker lifecycle ------------------------------------------------

    def _running(self) -> bool:
        """True if the broker socket answers. Never spawns."""
        if not self._sock.exists():
            return False
        try:
            ipc.get_property(self._sock, "idle-active", timeout=1.0)
            return True
        except (ipc.MpvIpcError, OSError):
            return False

    def _ensure_broker(self) -> None:
        if self._running():
            return
        if os.environ.get("MEDIA_BOOK_AUTOSPAWN", "1") == "0":
            raise ipc.MpvIpcError(
                "sink-book broker not running and autospawn disabled")
        # A stale socket from a dead broker would block the new bind.
        try:
            self._sock.unlink()
        except FileNotFoundError:
            pass
        self._sock.parent.mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        # mpv shells out to yt-dlp for YouTube; ensure ~/.local/bin is found.
        local_bin = str(Path.home() / ".local" / "bin")
        if local_bin not in env.get("PATH", "").split(os.pathsep):
            env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")

        client = os.environ.get("MEDIA_BOOK_AUDIO_CLIENT", "agent-media-book")
        argv = [
            os.environ.get("MEDIA_MPV_BIN", "mpv"),
            "--idle=yes", "--no-video", "--no-terminal", "--no-config",
            f"--input-ipc-server={self._sock}",
            f"--ao={os.environ.get('MEDIA_BOOK_AO', 'pulse')}",
            f"--audio-client-name={client}",
            "--cache=yes",
        ]
        # Optional richer browser UI: simple-mpv-webui loads INTO this mpv as a
        # Lua script (needs luasocket on the Lua-5.1 module path) and serves a
        # full audiobook UI — seek, speed, chapters, playlist. It binds 0.0.0.0,
        # but mel's firewall keeps it tailnet-only (the public zone exposes no
        # such port). Gated on the script existing; path/port/auth overridable.
        webui = os.environ.get(
            "MEDIA_BOOK_WEBUI",
            str(Path.home() / "src" / "simple-mpv-webui" / "main.lua"))
        if webui and Path(webui).is_file():
            opts = "webui-port=" + os.environ.get("MEDIA_BOOK_WEBUI_PORT", "8889")
            htpw = os.environ.get("MEDIA_BOOK_WEBUI_HTPASSWD", "")
            if htpw:
                opts += ",webui-htpasswd_path=" + htpw
            argv += [f"--script={webui}", f"--script-opts={opts}"]
        subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        # Wait for the socket to come up (cold mpv ~0.3-1s). EOF self-heal and
        # playlist auto-advance are handled by the book event watcher in the
        # long-lived MCP server (mcp_server._autoadvance_loop), which connects
        # to this socket — no per-broker sidecar here.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if self._running():
                return
            time.sleep(0.1)
        raise ipc.MpvIpcError("sink-book broker did not come up in time")

    # ---- playback --------------------------------------------------------

    def play(self, uri: str, target: Target = DEFAULT_TARGET,
             start_ms: Optional[int] = None, **_: object) -> str:
        """Load and play `uri` from `start_ms` (or the beginning).

        Returns the normalized URI handed to mpv (i.e. the bookmark key).
        """
        norm = normalize_uri(uri)
        self._ensure_broker()
        device = _device_for(target)
        if device is not None:
            try:
                ipc.set_property(self._sock, "audio-device", device)
            except ipc.MpvIpcError as e:
                log.warning("sink-book: set audio-device %s failed: %s", device, e)

        secs = max(0.0, (start_ms or 0) / 1000.0)
        if secs > 0:
            # mpv 0.37: loadfile <url> [<flags> [<options>]] — pass `start`
            # as an option so we resume without racing the async file-load.
            try:
                ipc.command(self._sock, "loadfile", norm, "replace",
                            f"start={secs:.3f}")
            except (ipc.MpvIpcError, OSError):
                # Arg-order differs across mpv versions; load then seek.
                ipc.command(self._sock, "loadfile", norm, "replace")
                self._seek_abs(secs)
        else:
            ipc.command(self._sock, "loadfile", norm, "replace")

        # Don't let a lingering pause/mute swallow the start.
        for prop in ("pause", "mute"):
            try:
                ipc.set_property(self._sock, prop, False)
            except (ipc.MpvIpcError, OSError):
                pass
        return norm

    def _seek_abs(self, secs: float) -> None:
        deadline = time.time() + 1.5
        while time.time() < deadline:
            try:
                ipc.set_property(self._sock, "time-pos", secs)
                return
            except (ipc.MpvIpcError, OSError):
                time.sleep(0.15)

    def pause(self, target: Target = DEFAULT_TARGET) -> None:
        try:
            ipc.set_property(self._sock, "pause", True)
        except (ipc.MpvIpcError, OSError):
            pass

    def resume(self, target: Target = DEFAULT_TARGET) -> None:
        try:
            ipc.set_property(self._sock, "pause", False)
        except (ipc.MpvIpcError, OSError):
            pass

    def stop(self, target: Target = DEFAULT_TARGET) -> None:
        try:
            ipc.command(self._sock, "stop")
        except (ipc.MpvIpcError, OSError):
            pass

    def skip(self, seconds: float, target: Target = DEFAULT_TARGET) -> None:
        """Seek ±seconds, clamped by mpv to the file bounds."""
        try:
            ipc.command(self._sock, "seek", seconds, "relative")
        except (ipc.MpvIpcError, OSError):
            pass

    def seek_to(self, seconds: float, target: Target = DEFAULT_TARGET) -> Optional[int]:
        """Seek to an absolute position (seconds from the start), clamped by
        mpv to the file bounds. Returns the resulting position in ms."""
        try:
            ipc.command(self._sock, "seek", max(0.0, float(seconds)), "absolute")
        except (ipc.MpvIpcError, OSError):
            return None
        return self.position(target)

    def set_speed(self, rate: float, target: Target = DEFAULT_TARGET) -> float:
        rate = max(_MIN_SPEED, min(_MAX_SPEED, float(rate)))
        try:
            ipc.set_property(self._sock, "speed", rate)
        except (ipc.MpvIpcError, OSError):
            pass
        return rate

    def speed(self, target: Target = DEFAULT_TARGET) -> Optional[float]:
        try:
            return float(ipc.get_property(self._sock, "speed"))
        except (ipc.MpvIpcError, TypeError, ValueError):
            return None

    def set_volume(self, level: int, target: Target = DEFAULT_TARGET) -> None:
        """Set the book stream volume (0-100). Phase-2 bed mixing uses this."""
        try:
            ipc.set_property(self._sock, "volume", max(0, min(100, level)))
        except (ipc.MpvIpcError, OSError):
            pass

    # ---- observation (spawn-free) ----------------------------------------

    def position(self, target: Target = DEFAULT_TARGET) -> Optional[int]:
        try:
            pos = ipc.get_property(self._sock, "time-pos")
        except (ipc.MpvIpcError, OSError):
            return None
        return int(pos * 1000) if pos is not None else None

    def duration(self, target: Target = DEFAULT_TARGET) -> Optional[int]:
        try:
            d = ipc.get_property(self._sock, "duration")
        except (ipc.MpvIpcError, OSError):
            return None
        return int(d * 1000) if d is not None else None

    def now_playing_uri(self, target: Target = DEFAULT_TARGET) -> Optional[str]:
        try:
            return ipc.get_property(self._sock, "path")
        except (ipc.MpvIpcError, OSError):
            return None

    def idle(self, target: Target = DEFAULT_TARGET) -> bool:
        """True when no broker is up or nothing is loaded. Never spawns."""
        if not self._running():
            return True
        try:
            return bool(ipc.get_property(self._sock, "idle-active"))
        except (ipc.MpvIpcError, OSError):
            return True

    def paused(self, target: Target = DEFAULT_TARGET) -> bool:
        try:
            return bool(ipc.get_property(self._sock, "pause"))
        except (ipc.MpvIpcError, OSError):
            return False

    def active(self, target: Target = DEFAULT_TARGET) -> bool:
        """True when a file is loaded and actually playing (not idle, not
        paused). Cheap and spawn-free — the coordinator uses this to decide
        whether a book needs pausing for speech.
        """
        return self._running() and not self.idle(target) and not self.paused(target)
