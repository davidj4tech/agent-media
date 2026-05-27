"""MPRIS pause/resume for non-Mopidy players via playerctl.

Enabled when MEDIA_MPRIS_PAUSE != "0" (default on).  Before speech,
pauses every playing MPRIS player except Mopidy (which the coordinator
already handles via MPD).  After speech, resumes only the ones we paused.

If playerctl is absent or returns errors the calls are silent no-ops so
the rest of the pipeline is unaffected.
"""

from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger(__name__)

_TIMEOUT = 2.0
_EXCLUDE_PREFIX = ("Mopidy",)


def _run(*args: str) -> str | None:
    try:
        r = subprocess.run(["playerctl", *args],
                           capture_output=True, text=True, timeout=_TIMEOUT)
        return (r.stdout or "").strip() if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def enabled() -> bool:
    return os.environ.get("MEDIA_MPRIS_PAUSE", "1") != "0"


def playing_players() -> list[str]:
    """Return names of MPRIS players currently in Playing state,
    excluding Mopidy (handled separately via MPD).
    """
    out = _run("--list-all")
    if not out:
        return []
    result = []
    for name in out.splitlines():
        name = name.strip()
        if not name:
            continue
        if any(name.startswith(ex) for ex in _EXCLUDE_PREFIX):
            continue
        status = _run("--player", name, "status")
        if status == "Playing":
            result.append(name)
    return result


def pause_players(names: list[str]) -> None:
    for name in names:
        _run("--player", name, "pause")
    if names:
        log.debug("mpris: paused %s", names)


def resume_players(names: list[str]) -> None:
    for name in names:
        _run("--player", name, "play")
    if names:
        log.debug("mpris: resumed %s", names)
