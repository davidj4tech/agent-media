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
  MEDIA_MUSIC_LOCAL_SSH       ssh host for the download helper (default p8a).
  MEDIA_MUSIC_LOCAL_FETCH     phone-side helper (default ``bin/play-local``).
  MEDIA_MUSIC_LOCAL_CACHE     phone cache dir relative to $HOME (default
                              ``.cache/music-offline``).
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
    return os.environ.get("MEDIA_MUSIC_LOCAL_SSH", "p8a")


def fetch_cmd() -> str:
    return os.environ.get("MEDIA_MUSIC_LOCAL_FETCH", "bin/play-local")


def cache_dir() -> str:
    return os.environ.get("MEDIA_MUSIC_LOCAL_CACHE", ".cache/music-offline")


def max_volume() -> int:
    """Ceiling for volume writes to the phone mpv.

    Not 100. The mpv-music service deliberately runs `--volume-max=170` with a
    default `--volume=130`, because 100 is *below* nominal on this device and
    everything sounded quiet. A hard clamp at 100 here silently destroyed that:
    every duck captured the live level, and the restore clamped it back to 100,
    so one spoken sentence permanently lowered the music and no amount of
    `media music volume +N` could lift it again.

    Mirror the service's ceiling instead. Overridable, and never below 100 so a
    bad value cannot make things quieter than the old behaviour.
    """
    try:
        return max(100, int(os.environ.get("MEDIA_MUSIC_LOCAL_VOLUME_MAX", "170")))
    except (TypeError, ValueError):
        return 170


def configured() -> bool:
    """True when a phone endpoint is set — gates the router and CLI/MCP routing."""
    return endpoint() is not None


def _watch_id(uri: str) -> Optional[str]:
    """Best-effort YouTube id extraction, shared with the rooms cache."""
    from . import music_fetch
    return music_fetch.watch_id(uri)


def _rooms_cached_path(vid: str) -> Optional[str]:
    from . import music_fetch
    return music_fetch.cached_path_for_id(vid)


def _phone_cached_path(vid: str) -> Optional[str]:
    host = ssh_host()
    cache = shlex.quote(cache_dir())
    qvid = shlex.quote(vid)
    remote = (f"ls -1 \"$HOME\"/{cache}/{qvid}.* 2>/dev/null | "
              "grep -v -e '\\.title$' -e '\\.part$' -e '\\.json$' | head -1")
    try:
        r = subprocess.run(["ssh", *_SSH_OPTS, host, remote],
                           capture_output=True, text=True, timeout=8)
    except (subprocess.TimeoutExpired, OSError):
        return None
    lines = [ln for ln in (r.stdout or "").strip().splitlines() if ln]
    return lines[0] if r.returncode == 0 and lines else None


def _rooms_reader(path: str) -> subprocess.Popen:
    from . import music_fetch
    rooms_host = music_fetch.rooms_ssh_host()
    if rooms_host is None:
        return subprocess.Popen(["cat", path], stdout=subprocess.PIPE)
    return subprocess.Popen(["ssh", *_SSH_OPTS, rooms_host,
                             f"cat {shlex.quote(path)}"],
                            stdout=subprocess.PIPE)


def _copy_rooms_to_phone(rooms_path: str) -> Optional[str]:
    """Seed the phone cache from rooms cache; return the phone-side path."""
    host = ssh_host()
    base = rooms_path.rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0]
    cache = shlex.quote(cache_dir())
    qbase, qstem = shlex.quote(base), shlex.quote(stem)
    reader = _rooms_reader(rooms_path)
    try:
        r = subprocess.run(
            ["ssh", *_SSH_OPTS, host,
             f"mkdir -p \"$HOME\"/{cache} && "
             f"cat > \"$HOME\"/{cache}/.{qbase}.part && "
             f"mv \"$HOME\"/{cache}/.{qbase}.part \"$HOME\"/{cache}/{qbase} && "
             f"echo \"$HOME\"/{cache}/{qbase}"],
            stdin=reader.stdout, capture_output=True, text=True, timeout=600)
    finally:
        reader.stdout.close()
        reader.wait()
    if reader.returncode != 0 or r.returncode != 0:
        log.warning("sink-music-local: rooms-to-phone cache copy failed: %s",
                    (r.stderr or "").strip()[-200:])
        return None
    phone_path = (r.stdout or "").strip().splitlines()[-1]
    sidecar = rooms_path.rsplit(".", 1)[0] + ".title"
    if os.path.exists(sidecar):
        title = subprocess.Popen(["cat", sidecar], stdout=subprocess.PIPE)
        try:
            subprocess.run(
                ["ssh", *_SSH_OPTS, host,
                 f"cat > \"$HOME\"/{cache}/{qstem}.title; "
                 f"[ -s \"$HOME\"/{cache}/{qstem}.title ] || "
                 f"rm -f \"$HOME\"/{cache}/{qstem}.title"],
                stdin=title.stdout, capture_output=True, text=True, timeout=30)
        finally:
            title.stdout.close()
            title.wait()
    return phone_path if phone_path.startswith("/") else None


def _phone_title(vid: str) -> str:
    host = ssh_host()
    remote = f"cat \"$HOME\"/{shlex.quote(cache_dir())}/{shlex.quote(vid)}.title 2>/dev/null"
    try:
        r = subprocess.run(["ssh", *_SSH_OPTS, host, remote],
                           capture_output=True, text=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def seed_from_rooms_cache(uri: str) -> Optional[str]:
    """Ensure the phone has rooms' cached copy of this YouTube item, if any."""
    vid = _watch_id(uri)
    if not vid:
        return None
    if cached := _phone_cached_path(vid):
        return cached
    rooms = _rooms_cached_path(vid)
    if not rooms:
        return None
    return _copy_rooms_to_phone(rooms)


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
        if seeded := seed_from_rooms_cache(uri):
            title = _phone_title(_watch_id(uri) or "")
            mode = "replace" if replace else "append-play"
            cmd = ["loadfile", seeded, mode]
            if title:
                opts = "force-media-title=%%%d%%%s" % (len(title.encode("utf-8")), title)
                cmd = ["loadfile", seeded, mode, -1, opts]
            ipc.command(self._endpoint(), *cmd)
            return

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
            self._set("volume", max(0, min(max_volume(), restore)))
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
                      max(0, min(max_volume(), int(round((cur or 100) + delta)))))
        except (ipc.MpvIpcError, OSError, TypeError, ValueError):
            pass

    def set_speed(self, rate: float, target: Target = DEFAULT_TARGET) -> bool:
        """Pitch-corrected playback speed on the phone mpv (0.25–4.0)."""
        try:
            self._set("speed", float(min(4.0, max(0.25, rate))))
            return True
        except (ipc.MpvIpcError, OSError):
            return False

    def current_speed(self, target: Target = DEFAULT_TARGET) -> Optional[float]:
        try:
            v = ipc.get_property(self._endpoint(), "speed")
            return float(v) if v is not None else None
        except (ipc.MpvIpcError, OSError, TypeError, ValueError):
            return None

    def current_volume(self, target: Target = DEFAULT_TARGET) -> Optional[int]:
        """The phone mpv's volume 0-100, or None when unreadable."""
        try:
            v = ipc.get_property(self._endpoint(), "volume")
            return int(round(float(v))) if v is not None else None
        except (ipc.MpvIpcError, OSError, TypeError, ValueError):
            return None

    # ---- observation (spawn-free; None/idle when the bridge is unreachable) --

    def position(self, target: Target = DEFAULT_TARGET) -> Optional[int]:
        try:
            pos = ipc.get_property(self._endpoint(), "time-pos")
        except (ipc.MpvIpcError, OSError):
            return None
        return int(pos * 1000) if pos is not None else None

    def now_playing_uri(self, target: Target = DEFAULT_TARGET) -> Optional[str]:
        # One pipelined round-trip, not two sequential ones: over the phone
        # bridge each round-trip is the whole cost of the call.
        try:
            p = ipc.get_properties(self._endpoint(), ["idle-active", "path"])
        except (ipc.MpvIpcError, OSError):
            return None
        if p.get("idle-active"):
            return None
        return p.get("path")

    def active(self, target: Target = DEFAULT_TARGET) -> bool:
        """True when the phone mpv has a file loaded and isn't paused. Cheap;
        the router uses this to decide whether phone-local is the live backend."""
        try:
            p = ipc.get_properties(self._endpoint(), ["idle-active", "pause"])
        except (ipc.MpvIpcError, OSError):
            return False
        if not p or p.get("idle-active"):
            return False
        return not bool(p.get("pause"))

    def loaded(self, target: Target = DEFAULT_TARGET) -> bool:
        """True when a file is loaded (playing OR paused) — so the coordinator
        can still duck/restore a phone track that's momentarily paused."""
        try:
            return not bool(ipc.get_property(self._endpoint(), "idle-active"))
        except (ipc.MpvIpcError, OSError):
            return False
