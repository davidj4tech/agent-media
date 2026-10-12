"""Linux: MPRIS through playerctl, PulseAudio/PipeWire sink names.

Exactly what the callers did before the seam: the player calls are
route/_mpris.py's (looked up at call time, so a test that patches that module
patches this too), a named sink is `pulse/<sink>`, mpv's AO is `pulse`.
"""

from __future__ import annotations

from typing import Optional

from . import AudioBackend


class LinuxBackend(AudioBackend):
    name = "linux"
    sink_plumbing = True

    def playing_players(self) -> list[str]:
        from ..route import _mpris
        return _mpris.playing_players()

    def pause_players(self, names: list[str]) -> None:
        from ..route import _mpris
        _mpris.pause_players(names)

    def resume_players(self, names: list[str]) -> None:
        from ..route import _mpris
        _mpris.resume_players(names)

    def default_ao(self) -> Optional[str]:
        return "pulse"

    def named_sink_device(self, sink: str) -> Optional[str]:
        return f"pulse/{sink}"
