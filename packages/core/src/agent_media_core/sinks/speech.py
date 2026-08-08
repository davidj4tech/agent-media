"""sink-speech: plays TTS clips through a long-running mpv broker.

The broker is started by the `sink-speech` runit service (or systemd
equivalent) with `--idle=yes --ao=openal --input-ipc-server=<socket>`.
This class talks to the socket; it does not spawn mpv itself.
"""

from __future__ import annotations

import logging
import os
import socket as _socket
import time
from pathlib import Path
from typing import Optional

from ..types import Target
from . import _mpv_ipc as ipc


log = logging.getLogger(__name__)

DEFAULT_TARGET = Target(name="local")

# Cross-host owner claim for a *shared remote* broker (the phone's mpv, driven
# by every host over the tcp:// bridge). The local playback flock only
# serializes one host; this token — stored in mpv `user-data` on the broker
# itself, so all hosts see the same value — stops a second machine's reply from
# stop+clearing another's still-playing playlist. Requires mpv >= 0.36.
_BROKER_OWNER_KEY = "user-data/am-owner"
# How long a claim stays valid without a refresh. Long enough to ride out a
# brief bridge hiccup, short enough that a crashed holder frees the broker soon.
BROKER_TTL_S = 20.0


def _broker_default_volume() -> float:
    """The broker's configured resting volume — the same MEDIA_SPEECH_VOLUME the
    `sink-speech` run script launches mpv with (default 150, louder than mpv's
    nominal 100). unduck restores to this so a duck cycle can't quietly pull
    speech below its intended level. Keep the two defaults in step: a mismatch
    means the first duck/unduck cycle silently re-levels the broker."""
    try:
        return float(os.environ.get("MEDIA_SPEECH_VOLUME", "150"))
    except (TypeError, ValueError):
        return 150.0


def _broker_max_volume() -> float:
    """The broker's --volume-max ceiling (MEDIA_SPEECH_VOLUME_MAX, default 200).
    Duck levels clamp to this rather than a bare 100 so they stay valid across
    the broker's amplified range."""
    try:
        return float(os.environ.get("MEDIA_SPEECH_VOLUME_MAX", "200"))
    except (TypeError, ValueError):
        return 200.0


def _broker_owner_id() -> str:
    """Stable id for this claimer. Same-host/same-pid callers are already
    serialized by the local flock, so host:pid is enough to tell hosts apart."""
    return f"{_socket.gethostname()}:{os.getpid()}"


def _socket_for(target: Target) -> "str | Path":
    """Resolve the IPC endpoint for a target (decision 1C).

    All targets share the single local mpv broker socket at
    `$XDG_STATE_HOME/agent-media/sink-speech.sock`; the *output device*
    is what differs per target (see `_device_for`). A per-target endpoint
    can be set with `MEDIA_SPEECH_SOCKET_<TARGET>` — either a Unix socket
    path, or a `tcp://host:port` to drive a *remote* mpv over a bridge
    (Grade B: red5 drives the phone's sink-speech). A tcp:// override is
    returned as a raw string — `Path()` would collapse `tcp://` to `tcp:/`.
    """
    override = os.environ.get(_env_key("MEDIA_SPEECH_SOCKET", target.name))
    if override:
        return override if override.startswith("tcp://") else Path(override)
    state = Path(os.environ.get("XDG_STATE_HOME",
                                str(Path.home() / ".local" / "state")))
    return state / "agent-media" / "sink-speech.sock"


def _env_key(prefix: str, target_name: str) -> str:
    return f"{prefix}_{target_name.upper().replace('-', '_')}"


def _clip_uri_for(uri: str, target: Target, prefer_url: bool = False) -> str:
    """Resolve the clip reference the *remote* player should load (Grade B).

    A remote broker (the phone's mpv over a TCP bridge) can't read this host's
    filesystem. Two ways to give it the clip, preferred in order:

      1. ``MEDIA_SPEECH_CLIP_LOCALDIR_<TARGET>`` — the clip was **pre-fetched**
         to this dir on the remote host (see :meth:`SinkSpeech.prefetch`); play
         it as a local file ``<localdir>/<basename>`` — no per-clip network I/O,
         which is what makes long replies reliable.
      2. ``MEDIA_SPEECH_CLIP_BASEURL_<TARGET>`` — fetch ``<baseurl>/<basename>``
         over HTTP (fallback; per-clip fetch, fragile on long replies).

    Already-URL uris and the all-unset case pass through, so local/rooms
    playback is unchanged.
    """
    if uri.startswith(("http://", "https://", "rtsp://")):
        return uri
    localdir = os.environ.get(_env_key("MEDIA_SPEECH_CLIP_LOCALDIR", target.name))
    base = os.environ.get(_env_key("MEDIA_SPEECH_CLIP_BASEURL", target.name))
    # prefer_url: the caller knows the localdir copy is unreliable (its
    # prefetch just failed), so a configured HTTP base beats a local path
    # that may not exist — a per-clip fetch is fragile, silence is worse.
    if localdir and not (prefer_url and base):
        return localdir.rstrip("/") + "/" + Path(uri).name
    if base:
        return base.rstrip("/") + "/" + Path(uri).name
    return uri


def _device_for(target: Target) -> Optional[str]:
    """Map a logical target to an mpv `audio-device` id (decision 1C).

    Returns None to leave the broker on its default device (the `local`
    case unless overridden). Routing is per-clip: `play` sets the device
    before `loadfile`, so one broker can serve `local`, `rooms`, etc.

    Resolution order:
      1. `MEDIA_SPEECH_DEVICE_<TARGET>` — explicit override for any
         target. "" / "auto" / "default" mean "broker default".
      2. `local`  → `MEDIA_SPEECH_LOCAL_DEVICE` (default: broker default).
      3. `rooms`  → `pulse/<MEDIA_ROOMS_SINK>` (default sink name `am`),
         i.e. the whole-house Snapcast feed.
    Unknown targets raise NotImplementedError so misroutes are loud.
    """
    override = os.environ.get(_env_key("MEDIA_SPEECH_DEVICE", target.name))
    if override is not None:
        return None if override.lower() in ("", "auto", "default") else override
    if target.name == "local":
        return os.environ.get("MEDIA_SPEECH_LOCAL_DEVICE") or None
    if target.name == "rooms":
        return f"pulse/{os.environ.get('MEDIA_ROOMS_SINK', 'am')}"
    raise NotImplementedError(
        f"sink-speech target {target.name!r} not configured — set "
        f"{_env_key('MEDIA_SPEECH_DEVICE', target.name)}")


class SinkSpeech:
    """Sink protocol implementation for the speech broker."""

    def __init__(self) -> None:
        # Target names whose last prefetch failed: play/play_playlist then
        # resolve clips to the HTTP base URL (if configured) instead of the
        # remote localdir the clips never reached.
        self._relay_unavailable: set = set()

    def _prefer_url(self, target: Target) -> bool:
        return target.name in self._relay_unavailable

    def play(self, uri: str, target: Target = DEFAULT_TARGET,
             reset_state: bool = True, **_: object) -> None:
        sock = _socket_for(target)
        device = _device_for(target)
        if device is not None:
            try:
                ipc.set_property(sock, "audio-device", device)
            except ipc.MpvIpcError as e:
                # Don't drop the clip over a device-switch hiccup; mpv
                # falls back to its current device.
                log.warning("sink-speech: set audio-device %s failed: %s",
                            device, e)
        # critical: this call IS the speech. A slow phone bridge must delay it,
        # never skip it — the breaker only exists to drop policy chatter.
        ipc.command(sock, "loadfile",
                    _clip_uri_for(uri, target, self._prefer_url(target)),
                    "replace", critical=True)
        # A fresh response must be audible regardless of a lingering
        # pause/mute left on the broker (e.g. a popup Space/m while idle) —
        # otherwise it loads into a paused/muted broker and plays silently.
        # But advancing between sentences of one response must NOT clear a
        # pause the user just made via the popup, or the response "resumes
        # itself". Callers pass reset_state=False for those mid-response
        # clips; only the first clip of a response resets.
        if reset_state:
            for prop in ("pause", "mute"):
                try:
                    ipc.set_property(sock, prop, False)
                except ipc.MpvIpcError:
                    pass

    def prefetch(self, paths: "list", target: Target = DEFAULT_TARGET) -> bool:
        """Copy all of a response's clips to the remote player's local dir up
        front (Grade B reliability).

        When ``MEDIA_SPEECH_CLIP_LOCALDIR_<TARGET>`` is set, the rendered clips
        are tar-piped over SSH into that dir on the remote host, so each
        subsequent `play` is a *local* loadfile instead of a per-clip network
        fetch — the per-sentence HTTP/​bridge fragility that stalled long replies.
        No-op (returns True) when no local dir is configured (local/rooms, or
        HTTP fallback). Best-effort: a failure is logged, remembered per
        target, and this sink's play/play_playlist then resolve clips to the
        HTTP base URL (if one is set) instead of the localdir the clips never
        reached — e.g. ssh to the phone broken while the clip HTTP server is
        fine. Returns False on failure so callers can react too.
        """
        localdir = os.environ.get(_env_key("MEDIA_SPEECH_CLIP_LOCALDIR", target.name))
        if not localdir or not paths:
            return True
        import shlex
        import subprocess
        host = (os.environ.get(_env_key("MEDIA_SPEECH_CLIP_SSH", target.name))
                or os.environ.get("MEDIA_MUSIC_LOCAL_SSH", "p8ar"))
        ps = [Path(p) for p in paths]
        srcdir = str(ps[0].parent)
        qnames = " ".join(shlex.quote(p.name) for p in ps)
        opts = ("-o BatchMode=yes -o ConnectTimeout=10 -o ControlMaster=auto "
                "-o ControlPath=/tmp/ssh-am-%r@%h:%p -o ControlPersist=300")
        remote = (f"mkdir -p {shlex.quote(localdir)} && "
                  f"tar -C {shlex.quote(localdir)} -xf -")
        # pipefail (hence bash): a tar-side failure (clip pruned from the
        # local cache) must not read as success just because ssh exited 0.
        cmd = (f"set -o pipefail; "
               f"tar -C {shlex.quote(srcdir)} -cf - {qnames} | "
               f"ssh {opts} {shlex.quote(host)} {shlex.quote(remote)}")
        try:
            rc = subprocess.run(
                cmd, shell=True, executable="/bin/bash",
                timeout=float(os.environ.get("MEDIA_SPEECH_PREFETCH_TIMEOUT", "30")),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            ).returncode
        except Exception as e:  # noqa: BLE001 — best-effort; play has its own fallback
            log.warning("sink-speech: prefetch to %s failed: %s", host, e)
            rc = -1
        if rc != 0:
            log.warning("sink-speech: prefetch to %s failed (rc=%s); "
                        "falling back to clip base URL if configured", host, rc)
            self._relay_unavailable.add(target.name)
            return False
        self._relay_unavailable.discard(target.name)
        return True

    def play_playlist(self, uris: "list", target: Target = DEFAULT_TARGET,
                      gapless: bool = True) -> None:
        """Load all of a response's clips as a gapless playlist and start it.

        The (remote) player then advances through the clips *autonomously* — no
        per-sentence drive from this host, so a bridge hiccup can't stall or cut
        the reply, and clips play back-to-back with no inter-sentence gap. The
        caller monitors `playlist_pos` to follow along (now_playing/highlight).
        """
        sock = _socket_for(target)
        device = _device_for(target)
        # One batched round-trip instead of ~10 (each a ~600ms hop over the
        # bridge). Build the whole playlist BEFORE starting: a `loadfile replace`
        # would play the (~0.5s) first clip *immediately*, and it can END before
        # the rest are appended, leaving mpv idle with unplayed items. So clear,
        # append every clip to the idle player (append does NOT auto-play), then
        # jump to index 0 — from there mpv auto-advances gaplessly.
        cmds: list = []
        if device is not None:
            cmds.append(["set_property", "audio-device", device])
        cmds.append(["set_property", "gapless-audio", "yes" if gapless else "no"])
        cmds.append(["stop"])
        cmds.append(["playlist-clear"])
        prefer_url = self._prefer_url(target)
        for uri in uris:
            cmds.append(["loadfile", _clip_uri_for(str(uri), target, prefer_url),
                         "append"])
        cmds.append(["set_property", "pause", False])
        cmds.append(["set_property", "mute", False])
        cmds.append(["set_property", "playlist-pos", 0])
        try:
            ipc.command_batch(sock, cmds, critical=True)
        except (ipc.MpvIpcError, OSError) as e:
            log.warning("sink-speech: play_playlist batch failed: %s", e)
            # The fallback chain is exhausted — this reply never sounded.
            # Queue a "missed speech" phone notification that retries until
            # the (probably dozed) phone wakes and can show it.
            try:
                from ._miss_notify import record_miss
                record_miss(target.name)
            except Exception:  # noqa: BLE001 — alerting must not break playback
                pass

    def snapshot(self, target: Target = DEFAULT_TARGET) -> dict:
        """One-round-trip read of the state the playlist monitor needs each tick
        (playlist-pos / idle-active / pause / time-pos). Empty dict on failure —
        far cheaper over a bridge than four separate get_property hops."""
        try:
            return ipc.get_properties(
                _socket_for(target),
                ["playlist-pos", "idle-active", "pause", "time-pos", "mute",
                 "speed"])
        except (ipc.MpvIpcError, OSError):
            return {}

    def playlist_pos(self, target: Target = DEFAULT_TARGET) -> Optional[int]:
        """Index of the clip the playlist is currently on (-1/None when idle)."""
        try:
            pos = ipc.get_property(_socket_for(target), "playlist-pos")
        except ipc.MpvIpcError:
            return None
        return int(pos) if pos is not None else None

    def set_playlist_pos(self, pos: int, target: Target = DEFAULT_TARGET) -> None:
        """Jump the playlist to clip `pos` (popup skip/replay over the bridge)."""
        try:
            ipc.set_property(_socket_for(target), "playlist-pos", int(pos))
        except ipc.MpvIpcError:
            pass

    def queue(self, uri: str, target: Target = DEFAULT_TARGET) -> None:
        """Append a clip to mpv's playlist without interrupting what's playing."""
        ipc.command(_socket_for(target), "loadfile",
                    _clip_uri_for(uri, target), "append", critical=True)

    def pause(self, target: Target = DEFAULT_TARGET) -> None:
        ipc.set_property(_socket_for(target), "pause", True)

    def resume(self, target: Target = DEFAULT_TARGET) -> None:
        ipc.set_property(_socket_for(target), "pause", False)

    def stop(self, target: Target = DEFAULT_TARGET) -> None:
        ipc.command(_socket_for(target), "stop")

    # ---- cross-host broker ownership -------------------------------------
    #
    # These are no-ops for a local/rooms (unix-socket) target: only one host
    # drives it, so the playback flock already serializes. They matter only for
    # a shared remote (tcp://) broker, where several hosts can drive the same
    # mpv and the flock — being per-host — can't stop them clobbering each other.

    def _is_remote(self, target: Target) -> bool:
        return str(_socket_for(target)).startswith("tcp://")

    def active_other_owner(self, target: Target = DEFAULT_TARGET) -> Optional[dict]:
        """The claim of *another* host that currently holds this broker, or None.

        None means 'safe to take': local target, unowned, expired, ours, or
        unreadable (a dead bridge — can't coordinate, so don't pretend someone
        holds it). Returns the raw ``{"owner", "deadline"}`` dict otherwise so a
        waiter can watch the deadline advance (a live holder refreshes it)."""
        if not self._is_remote(target):
            return None
        try:
            cur = ipc.get_property(_socket_for(target), _BROKER_OWNER_KEY)
        except (ipc.MpvIpcError, OSError):
            return None
        if not isinstance(cur, dict):
            return None
        owner = cur.get("owner")
        if not owner or owner == _broker_owner_id():
            return None
        try:
            if float(cur.get("deadline", 0)) <= time.time():
                return None  # expired — the holder crashed or stalled
        except (TypeError, ValueError):
            return None
        return cur

    def claim_broker(self, target: Target = DEFAULT_TARGET,
                     ttl: float = BROKER_TTL_S) -> bool:
        """Best-effort claim of a shared remote broker. Returns True once we own
        it — or immediately for a local target, or if the broker is unreachable
        (never block a reply on the token machinery itself)."""
        if not self._is_remote(target):
            return True
        if self.active_other_owner(target) is not None:
            return False  # someone else actively holds it
        sock = _socket_for(target)
        me = _broker_owner_id()
        try:
            ipc.set_property(sock, _BROKER_OWNER_KEY,
                             {"owner": me, "deadline": time.time() + ttl})
        except (ipc.MpvIpcError, OSError):
            return True  # can't reach broker to claim → play anyway, don't wedge
        # Verify after a small per-pid desync so two hosts that raced the read
        # don't both believe they won — the later writer wins and the other sees
        # it isn't the owner and backs off.
        time.sleep(0.05 + (os.getpid() % 10) / 100.0)
        try:
            cur = ipc.get_property(sock, _BROKER_OWNER_KEY)
        except (ipc.MpvIpcError, OSError):
            return True
        return isinstance(cur, dict) and cur.get("owner") == me

    def refresh_broker(self, target: Target = DEFAULT_TARGET,
                       ttl: float = BROKER_TTL_S) -> None:
        """Push our claim's deadline out while we keep playing. No-op unless we
        currently own it (so we never steal a claim from whoever took over)."""
        if not self._is_remote(target):
            return
        sock = _socket_for(target)
        me = _broker_owner_id()
        try:
            cur = ipc.get_property(sock, _BROKER_OWNER_KEY)
            if isinstance(cur, dict) and cur.get("owner") == me:
                ipc.set_property(sock, _BROKER_OWNER_KEY,
                                 {"owner": me, "deadline": time.time() + ttl})
        except (ipc.MpvIpcError, OSError):
            pass

    def release_broker(self, target: Target = DEFAULT_TARGET) -> None:
        """Drop our claim so the next host can take the broker immediately.
        Only clears it if it's still ours."""
        if not self._is_remote(target):
            return
        sock = _socket_for(target)
        me = _broker_owner_id()
        try:
            cur = ipc.get_property(sock, _BROKER_OWNER_KEY)
            if isinstance(cur, dict) and cur.get("owner") == me:
                ipc.set_property(sock, _BROKER_OWNER_KEY,
                                 {"owner": "", "deadline": 0})
        except (ipc.MpvIpcError, OSError):
            pass

    def duck(self, target: Target = DEFAULT_TARGET, level: int = 50) -> None:
        # Clamp to the broker's configured ceiling, not a bare 100 — the broker
        # runs with --volume-max above 100 for the louder default, so the duck
        # level must be free to sit anywhere in that range.
        ipc.set_property(_socket_for(target), "volume",
                         max(0, min(_broker_max_volume(), level)))

    def unduck(self, target: Target = DEFAULT_TARGET) -> None:
        # Restore to the broker's *configured* default, not a hardcoded 100,
        # or every duck/unduck cycle would quietly pull speech below the
        # louder MEDIA_SPEECH_VOLUME the broker launched with.
        ipc.set_property(_socket_for(target), "volume", _broker_default_volume())

    def position(self, target: Target = DEFAULT_TARGET) -> Optional[int]:
        try:
            pos = ipc.get_property(_socket_for(target), "time-pos")
        except ipc.MpvIpcError:
            return None
        if pos is None:
            return None
        return int(pos * 1000)

    def idle(self, target: Target = DEFAULT_TARGET) -> bool:
        """True when nothing is playing — useful for queue-vs-interrupt
        decisions in route/.
        """
        try:
            return bool(ipc.get_property(_socket_for(target), "idle-active"))
        except ipc.MpvIpcError:
            return True

    def paused(self, target: Target = DEFAULT_TARGET) -> bool:
        """True when a clip is loaded but held paused (e.g. popup Space).

        Returns False on IPC error so a transient hiccup can't wedge a
        caller that loops while paused.
        """
        try:
            return bool(ipc.get_property(_socket_for(target), "pause"))
        except ipc.MpvIpcError:
            return False

    def muted(self, target: Target = DEFAULT_TARGET) -> bool:
        """True when the speech broker is muted (e.g. popup `m`).

        Returns False on IPC error so a transient hiccup can't make a
        caller think silent-speech when it isn't.
        """
        try:
            return bool(ipc.get_property(_socket_for(target), "mute"))
        except ipc.MpvIpcError:
            return False
