"""sink-speech: plays TTS clips through a long-running mpv broker.

The broker is started by the `sink-speech` runit service (or systemd
equivalent) with `--idle=yes --ao=openal --input-ipc-server=<socket>`.
This class talks to the socket; it does not spawn mpv itself.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from ..types import Target
from . import _mpv_ipc as ipc


DEFAULT_TARGET = Target(name="local")


def _socket_for(target: Target) -> Path:
    """Resolve the IPC socket path for a target.

    Today only the `local` target is implemented; it lands at
    `$XDG_STATE_HOME/agent-media/sink-speech.sock`. Snapcast/BT targets
    will route through here later.
    """
    if target.name != "local":
        raise NotImplementedError(f"sink-speech target {target.name!r} not yet supported")
    state = Path(os.environ.get("XDG_STATE_HOME",
                                str(Path.home() / ".local" / "state")))
    return state / "agent-media" / "sink-speech.sock"


class SinkSpeech:
    """Sink protocol implementation for the speech broker."""

    def play(self, uri: str, target: Target = DEFAULT_TARGET, **_: object) -> None:
        ipc.command(_socket_for(target), "loadfile", uri, "replace")

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
