"""sink-music-local: the music channel's phone-local playout backend.

Datacenter hosts (mel/IONOS, red5/Hetzner) get HTTP 403 on most YouTube CDN
URLs, so they can't stream or download YouTube. The residential workaround is
to acquire and play the audio *on the phone*: download bestaudio on the phone's
residential IP (no 403, ~1/5 the bytes, cached for offline reuse) and play it on
the phone's local mpv.

Historically that path (`play-local`) was off-channel: it loaded the phone's
mpv over a private `agent-audio-relay` socket that agent-media knew nothing
about, so the speech coordinator never ducked it and it wasn't whole-house.

This backend makes phone-local playout a first-class member of the music
channel. It implements the same `Sink` contract as `SinkMusic` (Mopidy):

  - **play** runs the phone-side fetch+load helper over SSH (download must
    happen on the residential IP), so the bytes are acquired and loaded on the
    phone.
  - **duck / pause / resume / stop / position / now_playing_uri** talk to the
    phone's mpv over an IPC *bridge* — its Unix IPC socket exposed on a TCP port
    over Tailscale (see `_mpv_ipc` tcp:// support). That makes the coordinator's
    pre-speech duck reach the phone player like any other channel member.

Config (all overridable; backend is "unavailable" when the endpoint is unset):

  MEDIA_MUSIC_LOCAL_ENDPOINT  mpv IPC endpoint, e.g. ``tcp://100.94.14.59:6601``
                              (the phone's mpv-music.sock bridged to TCP).
  MEDIA_MUSIC_LOCAL_SSH       ssh host for the download helper (default p8ar).
  MEDIA_MUSIC_LOCAL_FETCH     phone-side helper (default ``bin/play-local``).
  MEDIA_MUSIC_LOCAL_FETCH_TIMEOUT  seconds to wait for download+load (default 120).
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from typing import Optional

from ..types import Target
from . import _mpv_ipc as ipc


log = logging.getLogger(__name__)

DEFAULT_TARGET = Target(name="phone")

# SSH options mirror route/_android.py: BatchMode + a persistent ControlMaster
# so repeat calls to the phone are cheap (no fresh TCP/auth handshake each time).
_SSH_OPTS = ["-o", "BatchMode=yes",
             "-o", "ConnectTimeout=8",
             "-o", "ControlMaster=auto",
             "-o", "ControlPath=/tmp/ssh-am-%r@%h:%p",
             "-o", "ControlPersist=300"]


def endpoint() -> Optional[str]:
    """The phone mpv IPC endpoint, or None when the backend isn't configured."""
    ep = os.environ.get("MEDIA_MUSIC_LOCAL_ENDPOINT", "").strip()
    return ep or None


def ssh_host() -> str:
    return os.environ.get("MEDIA_MUSIC_LOCAL_SSH", "p8ar")


def fetch_cmd() -> str:
    return os.environ.get("MEDIA_MUSIC_LOCAL_FETCH", "bin/play-local")


def configured() -> bool:
    """True when a phone endpoint is set — gates the router and CLI/MCP routing."""
    return endpoint() is not None


class SinkMusicLocal:
    """The music channel's phone-local backend (download-on-phone + local mpv)."""

    def __init__(self, ep: Optional[str] = None) -> None:
        # Resolve lazily on each call so env changes (settings reload) take
        # effect, but allow an explicit override for tests.
        self._ep_override = ep

    def _endpoint(self) -> str:
        ep = self._ep_override or endpoint()
        if not ep:
            raise ipc.MpvIpcError("sink-music-local: MEDIA_MUSIC_LOCAL_ENDPOINT unset")
        return ep

    # ---- playback (download happens on the phone, over SSH) --------------

    def play(self, uri: str, target: Target = DEFAULT_TARGET,
             replace: bool = True, **_: object) -> None:
        """Download `uri` on the phone (residential IP) and play it locally.

        Delegates acquisition+load to the phone-side helper over SSH because the
        download must originate on the residential IP. `replace=False` appends to
        the phone's mpv playlist instead of replacing it.
        """
        host = ssh_host()
        mode = "" if replace else " --add"
        # The helper accepts a bare URL or a yt: URI and handles the yt: strip.
        remote = f"{fetch_cmd()}{mode} {shlex.quote(uri)}"
        timeout = float(os.environ.get("MEDIA_MUSIC_LOCAL_FETCH_TIMEOUT", "120"))
        try:
            r = subprocess.run(
                ["ssh", *_SSH_OPTS, host, remote],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise ipc.MpvIpcError(f"sink-music-local: phone fetch timed out: {e}")
        except OSError as e:
            raise ipc.MpvIpcError(f"sink-music-local: ssh {host} failed: {e}")
        if r.returncode != 0:
            raise ipc.MpvIpcError(
                f"sink-music-local: phone fetch failed ({r.returncode}): "
                f"{(r.stderr or r.stdout).strip()[-300:]}")

    # ---- transport / duck (over the mpv IPC bridge) ----------------------

    def _set(self, name: str, value: object) -> None:
        ipc.set_property(self._endpoint(), name, value)

    def pause(self, target: Target = DEFAULT_TARGET) -> None:
        try:
            self._set("pause", True)
        except (ipc.MpvIpcError, OSError):
            pass

    def resume(self, target: Target = DEFAULT_TARGET) -> None:
        try:
            self._set("pause", False)
        except (ipc.MpvIpcError, OSError):
            pass

    def stop(self, target: Target = DEFAULT_TARGET) -> None:
        try:
            ipc.command(self._endpoint(), "stop")
        except (ipc.MpvIpcError, OSError):
            pass

    def duck(self, target: Target = DEFAULT_TARGET, level: int = 15) -> None:
        try:
            self._set("volume", max(0, min(100, level)))
        except (ipc.MpvIpcError, OSError):
            pass

    def unduck(self, target: Target = DEFAULT_TARGET, restore: int = 100) -> None:
        try:
            self._set("volume", max(0, min(100, restore)))
        except (ipc.MpvIpcError, OSError):
            pass

    def next(self, target: Target = DEFAULT_TARGET) -> None:
        try:
            ipc.command(self._endpoint(), "playlist-next", "weak")
        except (ipc.MpvIpcError, OSError):
            pass

    def previous(self, target: Target = DEFAULT_TARGET) -> None:
        try:
            ipc.command(self._endpoint(), "playlist-prev", "weak")
        except (ipc.MpvIpcError, OSError):
            pass

    def toggle(self, target: Target = DEFAULT_TARGET) -> None:
        try:
            ipc.command(self._endpoint(), "cycle", "pause")
        except (ipc.MpvIpcError, OSError):
            pass

    def seek_cur(self, target: Target = DEFAULT_TARGET, position_ms: int = 0) -> None:
        try:
            self._set("time-pos", max(0.0, position_ms / 1000.0))
        except (ipc.MpvIpcError, OSError):
            pass

    def seek_relative(self, secs: float, target: Target = DEFAULT_TARGET) -> None:
        try:
            ipc.command(self._endpoint(), "seek", float(secs), "relative")
        except (ipc.MpvIpcError, OSError):
            pass

    def volume_delta(self, delta: int, target: Target = DEFAULT_TARGET) -> None:
        try:
            cur = ipc.get_property(self._endpoint(), "volume")
            self._set("volume",
                      max(0, min(100, int(round((cur or 100) + delta)))))
        except (ipc.MpvIpcError, OSError, TypeError, ValueError):
            pass

    # ---- observation (spawn-free; None/idle when the bridge is unreachable) --

    def position(self, target: Target = DEFAULT_TARGET) -> Optional[int]:
        try:
            pos = ipc.get_property(self._endpoint(), "time-pos")
        except (ipc.MpvIpcError, OSError):
            return None
        return int(pos * 1000) if pos is not None else None

    def now_playing_uri(self, target: Target = DEFAULT_TARGET) -> Optional[str]:
        try:
            if ipc.get_property(self._endpoint(), "idle-active"):
                return None
            return ipc.get_property(self._endpoint(), "path")
        except (ipc.MpvIpcError, OSError):
            return None

    def active(self, target: Target = DEFAULT_TARGET) -> bool:
        """True when the phone mpv has a file loaded and isn't paused. Cheap;
        the router uses this to decide whether phone-local is the live backend."""
        try:
            if ipc.get_property(self._endpoint(), "idle-active"):
                return False
            return not bool(ipc.get_property(self._endpoint(), "pause"))
        except (ipc.MpvIpcError, OSError):
            return False

    def loaded(self, target: Target = DEFAULT_TARGET) -> bool:
        """True when a file is loaded (playing OR paused) — so the coordinator
        can still duck/restore a phone track that's momentarily paused."""
        try:
            return not bool(ipc.get_property(self._endpoint(), "idle-active"))
        except (ipc.MpvIpcError, OSError):
            return False
