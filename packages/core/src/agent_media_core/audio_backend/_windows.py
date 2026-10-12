"""Windows: the system media transport controls, WASAPI device names.

Pausing other players goes through Windows' own media controls
(`GlobalSystemMediaTransportControlsSessionManager`, the sessions the volume
flyout shows: Spotify, a browser tab, the Media Player app), reached in
process through the `winrt-Windows.Media.Control` projection — a Windows-only
dependency of core (~1.5 MB installed), chosen over shelling out to
PowerShell because that costs a PowerShell start (hundreds of ms, cold) on
every pause and every resume, both on the speech path, and depends on
Windows PowerShell 5.1's WinRT bridge (PowerShell 7 dropped it) and on the
language mode a managed machine allows.

A player's name is its app id (`source_app_user_model_id`, e.g. `Spotify.exe`),
with `#2`, `#3` for a second and third session of the same app. The session
objects from the last look are kept, so a resume reaches the very session
that was paused; one is resumed only while it is still Paused, as on Linux.

mpv's own sessions (an app id containing `mpv`) are never paused: a session
says nothing about which mpv it is, so — like Linux's fail-closed check —
every mpv is treated as one of our brokers. MEDIA_SMTC_EXCLUDE=a,b adds app
ids (case-insensitive substrings) to leave alone.

Output devices are mpv's WASAPI ids (`wasapi/{…}`); a configured value
without a `/` is matched against mpv's device descriptions, so
MEDIA_SPEECH_LOCAL_DEVICE="Headphones" finds `wasapi/{…}` (Headphones (…)).
There are no named sinks and no null-sink plumbing: `rooms` is a Linux hub's
feed, and reports itself not configured here.

Any failure — winrt missing, no desktop session, a call that does not finish
in time — leaves players as they are and is logged once.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Optional

from . import AudioBackend, log_once, output_devices

log = logging.getLogger(__name__)

_TIMEOUT = 2.0
#: GlobalSystemMediaTransportControlsSessionPlaybackStatus
_PLAYING = 4
_PAUSED = 5
#: Windows.Foundation.AsyncStatus.COMPLETED
_COMPLETED = 1
_OWN = ("mpv",)
#: After the manager could not be had, how long before asking again — a logon
#: task can start before the desktop session is ready to hand one out.
_RETRY_S = 60.0


def _exclusions() -> tuple[str, ...]:
    raw = os.environ.get("MEDIA_SMTC_EXCLUDE", "")
    return _OWN + tuple(s.strip().lower() for s in raw.split(",") if s.strip())


def _wait(op: Any, timeout: float = _TIMEOUT) -> Any:
    """The result of a WinRT IAsyncOperation; TimeoutError if it did not
    finish in `timeout` (it is left running — WinRT has no way to take it back)."""
    status = op.wait(timeout)
    if int(status) != _COMPLETED:
        raise TimeoutError(f"WinRT operation ended {status!r}")
    return op.get_results()


def _winrt_manager() -> Any:
    from winrt.windows.media.control import (  # type: ignore[import-not-found]
        GlobalSystemMediaTransportControlsSessionManager as Manager)
    # The sessions come back as an IVectorView, which is projected only once
    # the collections module is loaded.
    import winrt.windows.foundation.collections  # type: ignore[import-not-found]  # noqa: F401
    return _wait(Manager.request_async())


class WindowsBackend(AudioBackend):
    name = "windows"
    sink_plumbing = False

    def __init__(self, manager_factory: Optional[Callable[[], Any]] = None) -> None:
        self._factory = manager_factory or _winrt_manager
        self._manager: Any = None
        self._failed_at: Optional[float] = None
        self._lock = threading.Lock()
        #: name -> session, from the last playing_players() / pause_players()
        self._seen: dict[str, Any] = {}
        self._paused: dict[str, Any] = {}
        self._devices: dict[str, str] = {}

    # ---- media sessions --------------------------------------------------

    def _get_manager(self) -> Any:
        if self._manager is None:
            if (self._failed_at is not None
                    and time.monotonic() - self._failed_at < _RETRY_S):
                return None
            self._failed_at = time.monotonic()
            try:
                self._manager = self._factory()
                self._failed_at = None
            except ImportError as e:
                log_once("windows:winrt",
                         "audio: winrt media controls unavailable (%s); other "
                         "players are not paused for speech", e)
            except Exception as e:  # noqa: BLE001
                log_once("windows:manager",
                         "audio: media session manager unavailable (%s); other "
                         "players are not paused for speech", e)
        return self._manager

    def _sessions(self) -> list[tuple[str, Any]]:
        """`(name, session)` for every media session, in Windows' order."""
        mgr = self._get_manager()
        if mgr is None:
            return []
        try:
            sessions = list(mgr.get_sessions())
        except Exception as e:  # noqa: BLE001
            log_once("windows:sessions", "audio: listing media sessions failed: %s", e)
            return []
        out, counts = [], {}
        for s in sessions:
            try:
                app = str(s.source_app_user_model_id or "")
            except Exception:  # noqa: BLE001
                continue
            n = counts[app] = counts.get(app, 0) + 1
            out.append((app if n == 1 else f"{app}#{n}", s))
        return out

    @staticmethod
    def _status(session: Any) -> Optional[int]:
        try:
            return int(session.get_playback_info().playback_status)
        except Exception:  # noqa: BLE001
            return None

    def playing_players(self) -> list[str]:
        excl = _exclusions()
        result, seen = [], {}
        for name, s in self._sessions():
            if any(x in name.lower() for x in excl):
                continue
            if self._status(s) == _PLAYING:
                result.append(name)
                seen[name] = s
        with self._lock:
            self._seen = seen
        return result

    def _lookup(self, name: str, *cached: dict) -> Any:
        for d in cached:
            if name in d:
                return d[name]
        return next((s for n, s in self._sessions() if n == name), None)

    def pause_players(self, names: list[str]) -> None:
        paused = []
        for name in names:
            with self._lock:
                s = self._seen.get(name)
            s = s or self._lookup(name)
            if s is None:
                continue
            try:
                if _wait(s.try_pause_async()):
                    paused.append(name)
                    with self._lock:
                        self._paused[name] = s
            except Exception as e:  # noqa: BLE001
                log.debug("smtc: pause %s failed: %s", name, e)
        if paused:
            log.debug("smtc: paused %s", paused)

    def resume_players(self, names: list[str]) -> None:
        resumed = []
        for name in names:
            with self._lock:
                s = self._paused.pop(name, None)
            s = s or self._lookup(name)
            if s is None:
                log.debug("smtc: %s no longer registered, skipping resume", name)
                continue
            if self._status(s) != _PAUSED:
                continue
            try:
                if _wait(s.try_play_async()):
                    resumed.append(name)
            except Exception as e:  # noqa: BLE001
                log.debug("smtc: resume %s failed: %s", name, e)
        if resumed:
            log.debug("smtc: resumed %s", resumed)

    # ---- output ------------------------------------------------------------

    def default_ao(self) -> Optional[str]:
        return "wasapi"

    def named_sink_device(self, sink: str) -> Optional[str]:
        log_once("windows:sink",
                 "audio: no named sinks on Windows (%r asked); set the "
                 "target's MEDIA_*_DEVICE_<TARGET> to a wasapi/ device", sink)
        return None

    def device(self, spec: str) -> str:
        """A `wasapi/{…}` id as is; else a device description, matched."""
        if "/" in spec or spec.lower() in ("auto", "wasapi"):
            return spec
        with self._lock:
            hit = self._devices.get(spec)
        if hit:
            return hit
        want = spec.lower()
        devs = [(i, d) for i, d in output_devices() if i.startswith("wasapi/")]
        found = (next((i for i, d in devs if d.lower() == want), None)
                 or next((i for i, d in devs if want in d.lower()), None))
        if not found:
            log_once(f"windows:device:{spec}",
                     "audio: no output device matches %r; mpv gets it as is "
                     "(`mpv --audio-device=help` lists them)", spec)
            found = spec
        # Remembered either way: this runs before every clip, and asking mpv
        # costs a process start. A device plugged in later needs a restart.
        with self._lock:
            self._devices[spec] = found
        return found
