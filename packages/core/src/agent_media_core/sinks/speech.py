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


def _socket_for(target: Target) -> Path:
    """Resolve the IPC socket path for a target (decision 1C).

    All targets share the single local mpv broker socket at
    `$XDG_STATE_HOME/agent-media/sink-speech.sock`; the *output device*
    is what differs per target (see `_device_for`). A per-target socket
    can be set with `MEDIA_SPEECH_SOCKET_<TARGET>` for the future case
    of one broker per room playing simultaneously.
    """
    override = os.environ.get(_env_key("MEDIA_SPEECH_SOCKET", target.name))
    if override:
        return Path(override)
    state = Path(os.environ.get("XDG_STATE_HOME",
                                str(Path.home() / ".local" / "state")))
    return state / "agent-media" / "sink-speech.sock"


def _env_key(prefix: str, target_name: str) -> str:
    return f"{prefix}_{target_name.upper().replace('-', '_')}"


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

    def play(self, uri: str, target: Target = DEFAULT_TARGET, **_: object) -> None:
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
        ipc.command(sock, "loadfile", uri, "replace")
        # A new clip must be audible regardless of a lingering pause/mute
        # left on the broker (e.g. a popup Space/m while idle) — otherwise
        # every future clip loads into a paused/muted broker and plays
        # silently. The popup's keys still control the *currently* playing
        # clip; this only resets state at the start of a fresh one.
        for prop in ("pause", "mute"):
            try:
                ipc.set_property(sock, prop, False)
            except ipc.MpvIpcError:
                pass

    def queue(self, uri: str, target: Target = DEFAULT_TARGET) -> None:
        """Append a clip to mpv's playlist without interrupting what's playing."""
        ipc.command(_socket_for(target), "loadfile", uri, "append")

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
