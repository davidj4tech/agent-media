"""MPRIS pause/resume for non-Mopidy players via playerctl.

Enabled when MEDIA_MPRIS_PAUSE != "0" (default on).  Before speech,
pauses every playing MPRIS player except Mopidy (which the coordinator
already handles via MPD).  After speech, resumes only the ones we paused.

Remote hosts: set MEDIA_MPRIS_SSH_HOSTS=host1,host2 to also pause/resume
MPRIS players on remote machines via SSH.  Useful when TTS originates on
one host but browser media plays on another (e.g. mel TTS → sp4r Chrome).

If playerctl is absent or returns errors the calls are silent no-ops so
the rest of the pipeline is unaffected.
"""

from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger(__name__)

_TIMEOUT = 2.0
_SSH_TIMEOUT = 3.0
_EXCLUDE_PREFIX = ("Mopidy",)


def _run(*args: str) -> str | None:
    try:
        r = subprocess.run(["playerctl", *args],
                           capture_output=True, text=True, timeout=_TIMEOUT)
        return (r.stdout or "").strip() if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def _run_remote(host: str, *args: str) -> str | None:
    """Run playerctl on a remote host via SSH.

    Sets DBUS_SESSION_BUS_ADDRESS so playerctl can reach the user session
    without a full login shell.  BatchMode=yes prevents interactive prompts;
    ConnectTimeout caps stalls.
    """
    cmd = (
        f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus "
        f"playerctl {' '.join(args)}"
    )
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(_SSH_TIMEOUT)}",
             host, cmd],
            capture_output=True, text=True, timeout=_SSH_TIMEOUT + 1,
        )
        return (r.stdout or "").strip() if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def enabled() -> bool:
    return os.environ.get("MEDIA_MPRIS_PAUSE", "1") != "0"


def ssh_hosts() -> list[str]:
    """Hosts to also pause/resume via SSH (MEDIA_MPRIS_SSH_HOSTS=h1,h2)."""
    raw = os.environ.get("MEDIA_MPRIS_SSH_HOSTS", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


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


def remote_playing_players(host: str) -> list[str]:
    """Return names of Playing MPRIS players on a remote host."""
    out = _run_remote(host, "--list-all")
    if not out:
        return []
    result = []
    for name in out.splitlines():
        name = name.strip()
        if not name:
            continue
        if any(name.startswith(ex) for ex in _EXCLUDE_PREFIX):
            continue
        status = _run_remote(host, "--player", name, "status")
        if status == "Playing":
            result.append(name)
    return result


def pause_players(names: list[str]) -> None:
    for name in names:
        _run("--player", name, "pause")
    if names:
        log.debug("mpris: paused %s", names)


def pause_remote(host: str, names: list[str]) -> None:
    for name in names:
        _run_remote(host, "--player", name, "pause")
    if names:
        log.debug("mpris: paused %s on %s", names, host)


def resume_players(names: list[str]) -> None:
    """Resume players that were paused by pause_players.

    Chromium unregisters its MPRIS interface when paused then re-registers
    with a new instance suffix on the next interaction — so we can't rely
    on the exact name. Strategy:
      1. Try the exact stored name.
      2. Fall back to matching by base name (strip .instanceNNN suffix).
      3. Use play-pause (toggle) rather than play for broader compatibility.
      4. Only send if the current status is Paused to avoid double-toggling.
    """
    if not names:
        return
    current_out = _run("--list-all") or ""
    current = [n.strip() for n in current_out.splitlines() if n.strip()]

    resumed = []
    for name in names:
        target = name if name in current else _find_by_prefix(name, current)
        if not target:
            log.debug("mpris: %s no longer registered, skipping resume", name)
            continue
        if _run("--player", target, "status") == "Paused":
            _run("--player", target, "play-pause")
            resumed.append(target)
    if resumed:
        log.debug("mpris: resumed %s", resumed)


def resume_remote(host: str, names: list[str]) -> None:
    """Resume previously-paused players on a remote host."""
    if not names:
        return
    current_out = _run_remote(host, "--list-all") or ""
    current = [n.strip() for n in current_out.splitlines() if n.strip()]

    resumed = []
    for name in names:
        target = name if name in current else _find_by_prefix(name, current)
        if not target:
            log.debug("mpris: %s no longer registered on %s, skipping", name, host)
            continue
        if _run_remote(host, "--player", target, "status") == "Paused":
            _run_remote(host, "--player", target, "play-pause")
            resumed.append(target)
    if resumed:
        log.debug("mpris: resumed %s on %s", resumed, host)


def _find_by_prefix(name: str, current: list[str]) -> str | None:
    """Match a stored player name against the current list by base name,
    ignoring the .instanceNNN suffix that Chromium rotates on re-register.
    """
    base = name.split(".instance")[0] if ".instance" in name else name
    return next((n for n in current if n == base or n.startswith(base + ".")),
                None)
