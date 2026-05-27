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


_SSH_OPTS = ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(_SSH_TIMEOUT)}",
             "-o", "ControlMaster=auto",
             "-o", "ControlPath=/tmp/ssh-am-%r@%h:%p",
             "-o", "ControlPersist=120"]


def _ssh(host: str, script: str) -> str | None:
    """Run a shell script on a remote host via SSH (single connection).

    DBUS_SESSION_BUS_ADDRESS is exported inside the script so playerctl
    can reach the user D-Bus session without a full login shell.
    """
    dbus = "export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus"
    full = f"{dbus}\n{script}"
    try:
        r = subprocess.run(
            ["ssh", *_SSH_OPTS, host, "bash -s"],
            input=full,
            capture_output=True, text=True, timeout=_SSH_TIMEOUT + 2,
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


def warmup_remote(host: str) -> None:
    """Establish the SSH ControlMaster for host via a no-op connection.

    Call this in a background thread in parallel with TTS rendering so
    the ControlMaster socket is live before before_speech() needs it.
    """
    _ssh(host, "true")


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
    """Return names of Playing MPRIS players on a remote host (one SSH call)."""
    exclude = " ".join(f'"{p}"' for p in _EXCLUDE_PREFIX)
    script = f"""
exclude=({exclude})
for p in $(playerctl --list-all 2>/dev/null); do
    skip=0
    for ex in "${{exclude[@]}}"; do [[ "$p" == "$ex"* ]] && skip=1 && break; done
    [ $skip -eq 1 ] && continue
    [ "$(playerctl --player "$p" status 2>/dev/null)" = "Playing" ] && echo "$p"
done
"""
    out = _ssh(host, script)
    if not out:
        return []
    return [n for n in (l.strip() for l in out.splitlines()) if n]


def pause_players(names: list[str]) -> None:
    for name in names:
        _run("--player", name, "pause")
    if names:
        log.debug("mpris: paused %s", names)


def pause_remote(host: str, names: list[str]) -> None:
    if not names:
        return
    cmds = "\n".join(f'playerctl --player "{n}" pause 2>/dev/null' for n in names)
    _ssh(host, cmds)
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
    """Resume previously-paused players on a remote host (one SSH call).

    Uses the same prefix-match fallback as the local resume_players so
    Chromium instance rotation doesn't break resume.
    """
    if not names:
        return
    # Build a shell snippet that resolves each name (or its base-prefix
    # match against current players) and play-pauses if Paused.
    names_bash = " ".join(f'"{n}"' for n in names)
    script = f"""
stored=({names_bash})
current=$(playerctl --list-all 2>/dev/null)
for name in "${{stored[@]}}"; do
    # Exact match first, then base-prefix (strip .instanceNNN)
    target=""
    if echo "$current" | grep -qxF "$name"; then
        target="$name"
    else
        base=$(echo "$name" | sed 's/\\.instance[0-9]*//')
        target=$(echo "$current" | grep -m1 -E "^${{base}}(\\.[0-9]+)?$" || true)
    fi
    [ -z "$target" ] && continue
    [ "$(playerctl --player "$target" status 2>/dev/null)" = "Paused" ] || continue
    playerctl --player "$target" play-pause 2>/dev/null
    echo "resumed:$target"
done
"""
    out = _ssh(host, script) or ""
    resumed = [l.removeprefix("resumed:") for l in out.splitlines()
               if l.startswith("resumed:")]
    if resumed:
        log.debug("mpris: resumed %s on %s", resumed, host)


def _find_by_prefix(name: str, current: list[str]) -> str | None:
    """Match a stored player name against the current list by base name,
    ignoring the .instanceNNN suffix that Chromium rotates on re-register.
    """
    base = name.split(".instance")[0] if ".instance" in name else name
    return next((n for n in current if n == base or n.startswith(base + ".")),
                None)
