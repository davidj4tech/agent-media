"""The host's audio system, behind one narrow interface.

docs/proposals/2026-09-23-cross-platform.md, step 3. mpv plays everything and
runs everywhere; what does not travel is the machinery around it — pausing
other players while we speak (MPRIS/playerctl on Linux), the names of output
devices (`pulse/<sink>`), mpv's audio output driver, and the null-sink
plumbing `media-setup server` wires on a rooms hub. Callers ask here instead
of shelling out to Linux tools or checking `os.name`:

    backend().playing_players()    other players playing now, as names
    backend().pause_players(names) pause those
    backend().resume_players(names) resume the ones still paused
    backend().default_ao()         mpv `--ao` when none is configured, or None
    backend().named_sink_device(s) mpv `audio-device` for a named sink, or
                                   None where there are no named sinks
    backend().device(spec)         a configured device made into mpv's id
    backend().sink_plumbing        whether null/default-sink setup applies
    output_devices()               mpv's own device list, `(id, description)`

Three backends: `linux` (today's behaviour, unchanged — MPRIS via
route/_mpris.py, PulseAudio/PipeWire names), `windows` (the system media
transport controls through WinRT, WASAPI device names) and `none` (silent
no-ops, each logged once) for anything else. `MEDIA_AUDIO_BACKEND=linux|
windows|none` forces one. Every call answers "nothing" rather than raising:
the speech pipeline must never stop over a player it could not pause.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
from typing import Optional

log = logging.getLogger(__name__)

_logged: set[str] = set()
_logged_lock = threading.Lock()


def log_once(key: str, msg: str, *args: object) -> None:
    """Log `msg` at INFO the first time `key` is seen in this process."""
    with _logged_lock:
        if key in _logged:
            return
        _logged.add(key)
    log.info(msg, *args)


def pause_enabled() -> bool:
    """MEDIA_MPRIS_PAUSE=0 turns pausing other players off, on every backend.

    The name predates the seam; it stays so no host's env file has to change.
    """
    return os.environ.get("MEDIA_MPRIS_PAUSE", "1") != "0"


class AudioBackend:
    """The interface, and the `none` backend: nothing to pause, no names."""

    name = "none"
    #: Whether `media-setup server`'s null sinks / default sink apply here.
    sink_plumbing = False

    def playing_players(self) -> list[str]:
        log_once(f"{self.name}:players",
                 "audio: no media-player control on this platform (%s); "
                 "other players are not paused for speech", sys.platform)
        return []

    def pause_players(self, names: list[str]) -> None:
        return None

    def resume_players(self, names: list[str]) -> None:
        return None

    def default_ao(self) -> Optional[str]:
        """mpv `--ao` when none is configured. None = let mpv choose."""
        return None

    def named_sink_device(self, sink: str) -> Optional[str]:
        """mpv `audio-device` for a named sink (the rooms feed's `am`).

        None means this platform has no named sinks, and a target that needs
        one is reported as not configured.
        """
        log_once(f"{self.name}:sink",
                 "audio: no named sinks on this platform (%s); set the "
                 "target's MEDIA_*_DEVICE_<TARGET> to an mpv device", sys.platform)
        return None

    def device(self, spec: str) -> str:
        """A configured device value as mpv's `audio-device` id."""
        return spec


def _select() -> AudioBackend:
    forced = os.environ.get("MEDIA_AUDIO_BACKEND", "").strip().lower()
    plat = forced or ("windows" if sys.platform == "win32"
                      else "none" if sys.platform == "darwin"
                      else "linux")
    if plat == "linux":
        from ._linux import LinuxBackend
        return LinuxBackend()
    if plat == "windows":
        from ._windows import WindowsBackend
        return WindowsBackend()
    return AudioBackend()


_backend: Optional[AudioBackend] = None
_backend_lock = threading.Lock()


def backend() -> AudioBackend:
    """This host's backend, chosen once per process."""
    global _backend
    if _backend is None:
        with _backend_lock:
            if _backend is None:
                _backend = _select()
    return _backend


def reset() -> None:
    """Forget the chosen backend and the log-once marks (tests)."""
    global _backend
    with _backend_lock:
        _backend = None
    with _logged_lock:
        _logged.clear()


_DEVICE_LINE = re.compile(r"^\s*'([^']+)'\s+\((.*)\)\s*$")


def output_devices(mpv: Optional[str] = None,
                   timeout: float = 10.0) -> list[tuple[str, str]]:
    """mpv's audio devices as `(id, description)`, [] when mpv won't say.

    The id is what `--audio-device` / the `audio-device` property takes:
    `pulse/am` on Linux, `wasapi/{…}` on Windows.
    """
    exe = mpv or os.environ.get("MEDIA_MPV_BIN", "mpv")
    try:
        r = subprocess.run([exe, "--no-config", "--audio-device=help"],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in (r.stdout or "").splitlines():
        m = _DEVICE_LINE.match(line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out
