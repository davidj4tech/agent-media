"""sink-music-local: the music channel's phone-local playout backend.

Datacenter hosts (IONOS, Hetzner, ...) get HTTP 403 on most YouTube CDN
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
import socket
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


def is_self(host: str) -> bool:
    """True when `host` names the machine this code is running on.

    Every command in this module used to go over ssh unconditionally, which was
    right while the only caller was the hub: red5 asks the phone to fetch, the
    phone plays. The share listener broke that assumption — it runs `media
    music play --where phone` **on the phone**, so the phone ssh'd to itself,
    and the first real share out of the Android share sheet died on `Host key
    verification failed`.

    Fixing it by trusting the phone's own host key would work and would be
    wrong: ssh to yourself is a dependency on sshd, on a key, and on a
    known_hosts entry, for a subprocess that could just run. The sibling module
    already draws this line — `music_fetch.rooms_ssh_host()` returns None for
    loopback or this host and executes locally — so this is that rule, applied
    to the lane that had not needed it yet.

    Matches on the short name, like the sibling, plus the loopback spellings.
    That is enough for an ordinary host and NOT enough for the phone, which
    calls itself `localhost` whatever its tailnet name is — see
    `fetch_is_local`, which is what the phone lane actually asks.
    """
    host = (host or "").strip()
    if not host or host in ("127.0.0.1", "::1", "localhost"):
        return True
    return host.split(".")[0] == socket.gethostname().split(".")[0]


def host_argv(host: str, command: str) -> list:
    """argv that runs `command` on `host` — without ssh when that host is us.

    The command is a shell string either way, so the remote and local forms are
    the same string run by the same kind of shell.
    """
    if is_self(host):
        return ["sh", "-c", command]
    return ["ssh", *_SSH_OPTS, host, command]


def fetch_is_local() -> bool:
    """True when the phone-side helper should run here instead of over ssh.

    `is_self(ssh_host())` cannot answer this on the device that matters.
    **Android gives every Termux install the hostname `localhost`** — p8a does
    not know it is called p8a — so comparing `MEDIA_MUSIC_LOCAL_SSH` against
    `gethostname()` is False on the phone and False everywhere else, which is
    to say useless. That is why the first fix for the share sheet's `Host key
    verification failed` did not fix it.

    The endpoint answers instead, and cannot be wrong about it. This backend's
    whole identity is the phone's mpv IPC socket, and its *shape* says where
    that mpv is: a `tcp://` endpoint is the bridged form the hub reaches over
    Tailscale, while a bare path is a unix socket, reachable only from the
    device that owns it. If we can open the socket as a file, we are on the
    phone, and so is the helper that fetches for it.
    """
    if is_self(ssh_host()):
        return True
    ep = endpoint() or ""
    return bool(ep) and "://" not in ep


def phone_argv(command: str) -> list:
    """argv that runs a phone-side `command` — locally when we are the phone.

    The local form starts from `$HOME`, because the remote one does. `ssh host
    cmd` runs cmd in the login directory, so every command in this module is
    written against it — `MEDIA_MUSIC_LOCAL_FETCH` defaults to the *relative*
    `bin/play-local`, and the cache paths are `"$HOME"/...`. Dropping into
    `sh -c` without the `cd` inherits the caller's working directory instead,
    which for the share listener is wherever runit started it: the fetch died
    with `bin/play-local: not found` on a phone that has it.
    """
    if fetch_is_local():
        return ["sh", "-c", 'cd "$HOME" && ' + command]
    return ["ssh", *_SSH_OPTS, ssh_host(), command]


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


def nominal_volume() -> int:
    """What "normal" means on this backend — the level the mpv-music service
    starts at (`--volume=130`, see the ceiling note below).

    The coordinator falls back to this when it has no clean pre-duck reading to
    restore. Its own default is 45, a Mopidy-era number on a 0-100 dial; using
    that here is not a safe default but an audible drop the listener has to undo
    by hand, which is exactly what happened on 2026-08-14. Same env var the
    service reads, so the two cannot drift.
    """
    try:
        return max(1, int(os.environ.get("MEDIA_MUSIC_VOLUME", "130")))
    except (TypeError, ValueError):
        return 130


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


def _note_title(uri: str, title: str) -> None:
    """Give the play-history row for `uri` the name the cache knows.

    Best-effort and never fatal: the row is written when something is put on,
    when a URI is all anyone has, and this is the moment a real title becomes
    available. Failing to record it costs a nicer label in `media recent` and
    the phone's list, and nothing else.
    """
    if not title:
        return
    try:
        from ..state import StateStore
        StateStore().set_history_title("music", uri, title)
    except Exception:  # noqa: BLE001 — see the docstring
        log.debug("history title update failed for %s", uri, exc_info=True)


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
        r = subprocess.run(phone_argv(remote),
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
    return subprocess.Popen(host_argv(rooms_host, f"cat {shlex.quote(path)}"),
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
            phone_argv(
                      f"mkdir -p \"$HOME\"/{cache} && "
                      f"cat > \"$HOME\"/{cache}/.{qbase}.part && "
                      f"mv \"$HOME\"/{cache}/.{qbase}.part \"$HOME\"/{cache}/{qbase} && "
                      f"echo \"$HOME\"/{cache}/{qbase}"),
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
                phone_argv(
                          f"cat > \"$HOME\"/{cache}/{qstem}.title; "
                          f"[ -s \"$HOME\"/{cache}/{qstem}.title ] || "
                          f"rm -f \"$HOME\"/{cache}/{qstem}.title"),
                stdin=title.stdout, capture_output=True, text=True, timeout=30)
        finally:
            title.stdout.close()
            title.wait()
    return phone_path if phone_path.startswith("/") else None


def _phone_title(vid: str) -> str:
    host = ssh_host()
    remote = f"cat \"$HOME\"/{shlex.quote(cache_dir())}/{shlex.quote(vid)}.title 2>/dev/null"
    try:
        r = subprocess.run(phone_argv(remote),
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
            self._unpause_if(replace)
            _note_title(uri, title)
            return

        host = ssh_host()
        mode = "" if replace else " --add"
        # The helper accepts a bare URL or a yt: URI and handles the yt: strip.
        remote = f"{fetch_cmd()}{mode} {shlex.quote(uri)}"
        timeout = float(os.environ.get("MEDIA_MUSIC_LOCAL_FETCH_TIMEOUT", "120"))
        try:
            r = subprocess.run(
                phone_argv(remote),
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise ipc.MpvIpcError(f"sink-music-local: phone fetch timed out: {e}")
        except OSError as e:
            raise ipc.MpvIpcError(
                f"sink-music-local: fetch on {host} failed: {e}"
                if not is_self(host)
                else f"sink-music-local: local fetch failed: {e}")
        if r.returncode != 0:
            raise ipc.MpvIpcError(
                f"sink-music-local: phone fetch failed ({r.returncode}): "
                f"{(r.stderr or r.stdout).strip()[-300:]}")
        self._unpause_if(replace)
        # The helper writes a `.title` sidecar beside the audio it just
        # fetched, so the real name exists by the time this returns — and the
        # history row written a moment ago has only the URI.
        _note_title(uri, _phone_title(_watch_id(uri) or ""))

    def _unpause_if(self, replace: bool) -> None:
        """Clear a lingering pause after a play that replaces the queue.

        `loadfile … replace` into a paused player loads the track and leaves it
        exactly where the pause was: at 0:00, silent. So a tap on a music row in
        the phone's history put the right thing in the player and nothing came
        out, which reads as the tap having done nothing at all — the player was
        paused hours earlier, by a transport button or a duck that never
        restored, and "play" is not supposed to remember that.

        The speech sink has cleared pause on every fresh clip since the first
        week, for exactly this reason and in these words. Music never learned it.

        Only when the queue is being replaced: `--add` means enqueue, and
        starting a paused player because something was added to the end of it
        would be a different verb than the one that was asked for.

        Best-effort — the load already happened, and a failure here is a track
        sitting paused rather than an exception in a caller that was told to
        play something.
        """
        if not replace:
            return
        try:
            self._set("pause", False)
        except (ipc.MpvIpcError, OSError) as e:
            log.debug("sink-music-local: could not clear pause: %s", e)

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

    def nominal_volume(self, target: Target = DEFAULT_TARGET) -> int:
        """This backend's normal listening level; see module-level
        :func:`nominal_volume`."""
        return nominal_volume()

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
