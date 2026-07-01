"""sink-speech: plays TTS clips through a long-running mpv broker.

The broker is started by the `sink-speech` runit service (or systemd
equivalent) with `--idle=yes --ao=openal --input-ipc-server=<socket>`.
This class talks to the socket; it does not spawn mpv itself.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from ..types import Target
from . import _mpv_ipc as ipc


log = logging.getLogger(__name__)

DEFAULT_TARGET = Target(name="local")


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


def _clip_uri_for(uri: str, target: Target) -> str:
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
    if localdir:
        return localdir.rstrip("/") + "/" + Path(uri).name
    base = os.environ.get(_env_key("MEDIA_SPEECH_CLIP_BASEURL", target.name))
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
        ipc.command(sock, "loadfile", _clip_uri_for(uri, target), "replace")
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

    def prefetch(self, paths: "list", target: Target = DEFAULT_TARGET) -> None:
        """Copy all of a response's clips to the remote player's local dir up
        front (Grade B reliability).

        When ``MEDIA_SPEECH_CLIP_LOCALDIR_<TARGET>`` is set, the rendered clips
        are tar-piped over SSH into that dir on the remote host, so each
        subsequent `play` is a *local* loadfile instead of a per-clip network
        fetch — the per-sentence HTTP/​bridge fragility that stalled long replies.
        No-op when no local dir is configured (local/rooms, or HTTP fallback).
        Best-effort: a failure is logged and play falls back to whatever
        `_clip_uri_for` resolves (the HTTP URL if a base is set).
        """
        localdir = os.environ.get(_env_key("MEDIA_SPEECH_CLIP_LOCALDIR", target.name))
        if not localdir or not paths:
            return
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
        cmd = (f"tar -C {shlex.quote(srcdir)} -cf - {qnames} | "
               f"ssh {opts} {shlex.quote(host)} {shlex.quote(remote)}")
        try:
            subprocess.run(
                cmd, shell=True,
                timeout=float(os.environ.get("MEDIA_SPEECH_PREFETCH_TIMEOUT", "30")),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except Exception as e:  # noqa: BLE001 — best-effort; play has its own fallback
            log.warning("sink-speech: prefetch to %s failed: %s", host, e)

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
        for uri in uris:
            cmds.append(["loadfile", _clip_uri_for(str(uri), target), "append"])
        cmds.append(["set_property", "pause", False])
        cmds.append(["set_property", "mute", False])
        cmds.append(["set_property", "playlist-pos", 0])
        try:
            ipc.command_batch(sock, cmds)
        except (ipc.MpvIpcError, OSError) as e:
            log.warning("sink-speech: play_playlist batch failed: %s", e)

    def snapshot(self, target: Target = DEFAULT_TARGET) -> dict:
        """One-round-trip read of the state the playlist monitor needs each tick
        (playlist-pos / idle-active / pause / time-pos). Empty dict on failure —
        far cheaper over a bridge than four separate get_property hops."""
        try:
            return ipc.get_properties(
                _socket_for(target),
                ["playlist-pos", "idle-active", "pause", "time-pos", "mute"])
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
                    _clip_uri_for(uri, target), "append")

    def pause(self, target: Target = DEFAULT_TARGET) -> None:
        ipc.set_property(_socket_for(target), "pause", True)

    def resume(self, target: Target = DEFAULT_TARGET) -> None:
        ipc.set_property(_socket_for(target), "pause", False)

    def stop(self, target: Target = DEFAULT_TARGET) -> None:
        ipc.command(_socket_for(target), "stop")

    def duck(self, target: Target = DEFAULT_TARGET, level: int = 50) -> None:
        ipc.set_property(_socket_for(target), "volume", max(0, min(100, level)))

    def unduck(self, target: Target = DEFAULT_TARGET) -> None:
        ipc.set_property(_socket_for(target), "volume", 100)

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
