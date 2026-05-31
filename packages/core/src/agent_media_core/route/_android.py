"""Android media pause/resume via SSH + `cmd media_session dispatch`.

For Android phones (typically Termux + sshd) which don't expose MPRIS.
The approach: SSH in and dispatch media keys to the active session with
`cmd media_session dispatch pause|play`. Unlike `am broadcast MEDIA_BUTTON`
(which modern Android sends "without waiting for result" and routinely fails
to route to the active session), dispatch reaches the foreground media
session — and, importantly, needs no special permission, so it works on
stock non-root Termux. `dispatch pause` is a safe no-op when nothing is
playing (it won't start playback).

State detection (`dumpsys media_session` / `cmd media_session list-sessions`)
requires the privileged DUMP / MEDIA_CONTENT_CONTROL permission and is denied
on non-root Termux, so we can't reliably tell what's playing — we just
dispatch pause before speech and play after (matching the MPRIS path's
pause/resume), gated by is_playing() which defaults to True on denial.

Enabled by setting MEDIA_ANDROID_PAUSE_HOSTS=phone (comma-separated for
multiple hosts). Cooperates with the MPRIS path in `_mpris.py` — both can
be active for different remote hosts in the same response.

Override the dispatch commands with MEDIA_ANDROID_PAUSE_CMD /
MEDIA_ANDROID_RESUME_CMD (e.g. for ADB, `input keyevent`, or a rooted phone).
Set MEDIA_ANDROID_RESUME=0 to pause without auto-resuming.
"""

from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger(__name__)


_SSH_CONNECT_TIMEOUT = 8
_SSH_CMD_TIMEOUT = 12.0
_SSH_CONTROL_PERSIST = 300

_SSH_OPTS = ["-o", "BatchMode=yes",
             "-o", f"ConnectTimeout={_SSH_CONNECT_TIMEOUT}",
             "-o", "ControlMaster=auto",
             "-o", "ControlPath=/tmp/ssh-am-%r@%h:%p",
             "-o", f"ControlPersist={_SSH_CONTROL_PERSIST}"]


# Dispatch explicit pause / play keys to the active media session. Works
# without root or extra permissions, and reaches the foreground session
# (unlike an MEDIA_BUTTON broadcast).
_DEFAULT_PAUSE_CMD = "cmd media_session dispatch pause >/dev/null 2>&1"
_DEFAULT_RESUME_CMD = "cmd media_session dispatch play >/dev/null 2>&1"


def _ssh(host: str, script: str) -> str | None:
    try:
        r = subprocess.run(
            ["ssh", *_SSH_OPTS, host, "sh -s"],
            input=script,
            capture_output=True, text=True, timeout=_SSH_CMD_TIMEOUT,
        )
        return (r.stdout or "").strip() if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def pause_hosts() -> list[str]:
    """Hosts to pause via media-button SSH (MEDIA_ANDROID_PAUSE_HOSTS=h1,h2)."""
    raw = os.environ.get("MEDIA_ANDROID_PAUSE_HOSTS", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


def pause_cmd() -> str:
    return os.environ.get("MEDIA_ANDROID_PAUSE_CMD", _DEFAULT_PAUSE_CMD)


def resume_cmd() -> str:
    return os.environ.get("MEDIA_ANDROID_RESUME_CMD", _DEFAULT_RESUME_CMD)


def resume_enabled() -> bool:
    return os.environ.get("MEDIA_ANDROID_RESUME", "1") != "0"


def is_playing(host: str) -> bool:
    """True if dumpsys media_session reports any active playing session.

    On Termux without root, `dumpsys` is denied (no DUMP permission) — we
    return True in that case so the toggle still fires. Set
    MEDIA_ANDROID_REQUIRE_PLAYING_DETECTION=1 to require positive detection
    (no toggle when state can't be determined).
    """
    out = _ssh(host, "/system/bin/dumpsys media_session 2>&1")
    if out is None:
        return False
    # Look for "state=N" where N is 3 (PLAYING) or 8 (BUFFERING).
    for line in out.splitlines():
        if "PlaybackState" in line or "state=" in line:
            if "state=3" in line or "state=8" in line:
                return True
    # If we couldn't read state (permission denied / no dumpsys), default
    # to True so the toggle still fires for users on stock Termux.
    if "Permission Denial" in out or "not found" in out or not out.strip():
        if os.environ.get("MEDIA_ANDROID_REQUIRE_PLAYING_DETECTION") == "1":
            return False
        log.debug("android: %s can't detect state (permission/missing); "
                  "defaulting to play-pause toggle", host)
        return True
    return False


def warmup(host: str) -> None:
    """Establish the SSH ControlMaster via a no-op command."""
    _ssh(host, "true")


def pause(host: str) -> None:
    """Pause the active media session on the host (safe no-op if idle)."""
    _ssh(host, pause_cmd())
    log.debug("android: dispatched pause to %s", host)


def resume(host: str) -> None:
    """Resume the active media session on the host."""
    if not resume_enabled():
        return
    _ssh(host, resume_cmd())
    log.debug("android: dispatched play to %s", host)
