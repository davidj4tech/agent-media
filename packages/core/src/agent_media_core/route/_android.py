"""Android media pause/resume via SSH + media-button intent.

For Android phones (typically Termux + sshd) which don't expose MPRIS.
The approach: SSH in, query `dumpsys media_session` for an active playing
session, and if so, send a media-button intent (KEYCODE_MEDIA_PLAY_PAUSE)
that most apps (Spotify, YouTube Music, Pocket Casts, etc.) register for.

Enabled by setting MEDIA_ANDROID_PAUSE_HOSTS=phone (comma-separated for
multiple hosts). Cooperates with the MPRIS path in `_mpris.py` — both can
be active for different remote hosts in the same response.

Defaults assume Termux can issue `am broadcast`. If your phone needs a
different command (input keyevent, termux-keyevent via termux-api, ADB,
shizuku, etc.) override with MEDIA_ANDROID_PAUSE_CMD.
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


# Send KEYCODE_MEDIA_PLAY_PAUSE (85) as a media-button broadcast.
# Most media apps register a MediaButtonReceiver and respond.
_DEFAULT_PLAY_PAUSE_CMD = (
    "am broadcast -a android.intent.action.MEDIA_BUTTON "
    "--ei android.intent.extra.KEY_EVENT 85 >/dev/null 2>&1"
)


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


def play_pause_cmd() -> str:
    return os.environ.get("MEDIA_ANDROID_PAUSE_CMD", _DEFAULT_PLAY_PAUSE_CMD)


def is_playing(host: str) -> bool:
    """True if dumpsys media_session reports any active session in a
    playing-ish state (PlaybackState state=3 PLAYING or state=8 BUFFERING).
    """
    out = _ssh(host, "dumpsys media_session 2>/dev/null")
    if not out:
        return False
    # Look for "state=N" where N is 3 (PLAYING) or 8 (BUFFERING).
    for line in out.splitlines():
        if "PlaybackState" in line or "state=" in line:
            if "state=3" in line or "state=8" in line:
                return True
    return False


def warmup(host: str) -> None:
    """Establish the SSH ControlMaster via a no-op command."""
    _ssh(host, "true")


def send_play_pause(host: str) -> None:
    """Toggle play/pause on the host. Used for both pause-when-playing and
    resume-when-was-paused — Android exposes only the toggle.
    """
    _ssh(host, play_pause_cmd())
    log.debug("android: sent play-pause to %s", host)
