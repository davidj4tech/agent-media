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
    """Rewrite a local clip path to a fetchable URL for a remote-played target.

    When a target's broker is on another host (e.g. the phone's mpv reached over
    a TCP bridge — Grade B), it can't read *this* host's filesystem. If
    ``MEDIA_SPEECH_CLIP_BASEURL_<TARGET>`` is set, a local file path is rewritten
    to ``<baseurl>/<basename>`` so the remote mpv fetches the clip over HTTP
    (red5 serves the audio dir on the tailnet). Already-URL uris and the unset
    case pass through, so local/rooms playback is unchanged.
    """
    base = os.environ.get(_env_key("MEDIA_SPEECH_CLIP_BASEURL", target.name))
    if not base or uri.startswith(("http://", "https://", "rtsp://")):
        return uri
    return base.rstrip("/") + "/" + Path(uri).name


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
