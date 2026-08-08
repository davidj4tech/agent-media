"""Android media pause/resume via SSH + `cmd media_session dispatch`.

For Android phones (typically Termux + sshd) which don't expose MPRIS.
The approach: SSH in and dispatch media keys to the active session with
`cmd media_session dispatch pause|play`. Unlike `am broadcast MEDIA_BUTTON`
(which modern Android sends "without waiting for result" and routinely fails
to route to the active session), dispatch reaches the foreground media
session — and, importantly, needs no special permission, so it works on
stock non-root Termux. `dispatch pause` is a safe no-op when nothing is
playing (it won't start playback) — but `dispatch play` is NOT a no-op: it
will *start* a session. Resume is therefore asymmetric and gated carefully.

State detection (`dumpsys media_session`) needs the privileged DUMP
permission, which is denied on non-root Termux. So we distinguish three
cases (see `playback_state` / `pause_for_speech`):
  - playing  → pause before speech, resume after.
  - stopped  → leave it alone (nothing to pause).
  - unknown  → (permission denied / host unreachable) dispatch pause
               defensively (a safe no-op when idle) but do NOT auto-resume,
               since `play` could start a session that was never playing.
               Set MEDIA_ANDROID_RESUME_ON_UNKNOWN=1 to resume anyway, or
               MEDIA_ANDROID_REQUIRE_PLAYING_DETECTION=1 to skip unknown
               hosts entirely (no pause either).

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
import time

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
# (unlike an MEDIA_BUTTON broadcast). Absolute path: non-interactive SSH on
# Termux usually lacks /system/bin on PATH (same reason dumpsys is absolute).
_DEFAULT_PAUSE_CMD = "/system/bin/cmd media_session dispatch pause >/dev/null 2>&1"
_DEFAULT_RESUME_CMD = "/system/bin/cmd media_session dispatch play >/dev/null 2>&1"


# --- slow-host breaker -----------------------------------------------------
#
# Same reasoning as the mpv bridge breaker: a phone on a lossy 400ms link costs
# ~3s per SSH command, and speech was paying that twice per sentence before a
# word came out. Pausing the phone's media is best-effort by nature (every call
# site already tolerates failure), so when a host proves slow, stop asking it
# for a while rather than making the user wait on it every time.

_NS = "android"
_slow_until: dict[str, float] | None = None      # lazy; unix-epoch deadlines


def _state() -> dict[str, float]:
    """Breaker deadlines, loaded once per process from the shared store.

    Shared on disk because speech is produced by short-lived processes (the
    Stop hook forks per reply), so an in-memory breaker would start cold and
    re-pay the discovery cost on every single utterance.
    """
    global _slow_until
    if _slow_until is None:
        from .. import _breaker
        _slow_until = _breaker.load(_NS)
    return _slow_until


def _slow_s() -> float:
    try:
        return float(os.environ.get("MEDIA_ANDROID_SLOW_MS", "1500")) / 1000
    except ValueError:
        return 1.5


def _breaker_s() -> float:
    """Cool-off for a slow host. 0 disables the breaker."""
    try:
        return float(os.environ.get("MEDIA_ANDROID_BREAKER_S", "20"))
    except ValueError:
        return 20.0


def reset_breaker(host: str | None = None) -> None:
    """Forget breaker state — for tests, and for an explicit user retry."""
    from .. import _breaker
    state = _state()
    if host is None:
        state.clear()
        _breaker.clear(_NS)
    else:
        state.pop(host, None)
        _breaker.store(_NS, state)


def _ssh(host: str, script: str) -> str | None:
    state = _state()
    until = state.get(host)
    if until and time.time() < until:
        return None                      # same contract as an unreachable host
    t0 = time.monotonic()
    out: str | None = None
    try:
        r = subprocess.run(
            ["ssh", *_SSH_OPTS, host, "sh -s"],
            input=script,
            capture_output=True, text=True, timeout=_SSH_CMD_TIMEOUT,
        )
        out = (r.stdout or "").strip() if r.returncode == 0 else None
        return out
    except Exception:  # noqa: BLE001
        return None
    finally:
        window = _breaker_s()
        if window > 0:
            slow = out is None or time.monotonic() - t0 >= _slow_s()
            was_open = host in state
            if slow:
                state[host] = time.time() + window
            elif was_open:
                state.pop(host, None)
            if slow or was_open:
                from .. import _breaker
                _breaker.store(_NS, state)


def pause_hosts() -> list[str]:
    """Hosts to pause via SSH dispatch (MEDIA_ANDROID_PAUSE_HOSTS=h1,h2)."""
    raw = os.environ.get("MEDIA_ANDROID_PAUSE_HOSTS", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


def pause_cmd() -> str:
    return os.environ.get("MEDIA_ANDROID_PAUSE_CMD", _DEFAULT_PAUSE_CMD)


def resume_cmd() -> str:
    return os.environ.get("MEDIA_ANDROID_RESUME_CMD", _DEFAULT_RESUME_CMD)


def resume_enabled() -> bool:
    return os.environ.get("MEDIA_ANDROID_RESUME", "1") != "0"


def resume_on_unknown() -> bool:
    """Whether to auto-resume hosts whose playback state couldn't be read.

    Off by default: `dispatch play` *starts* a session, so resuming on a
    guess risks spawning playback that was never there. Opt in on a host you
    trust to have been playing (e.g. a dedicated music phone).
    """
    return os.environ.get("MEDIA_ANDROID_RESUME_ON_UNKNOWN", "0") == "1"


def require_detection() -> bool:
    """If set, only act on hosts whose playback state is positively known."""
    return os.environ.get("MEDIA_ANDROID_REQUIRE_PLAYING_DETECTION") == "1"


def playback_state(host: str) -> str:
    """Best-effort media playback state: 'playing', 'stopped', or 'unknown'.

    Reads `dumpsys media_session` (absolute path — non-interactive SSH on
    Termux usually lacks /system/bin on PATH). 'unknown' covers both an
    unreachable host and the common stock-Termux case where the DUMP
    permission is denied, so state can't be read at all.
    """
    out = _ssh(host, "/system/bin/dumpsys media_session 2>&1")
    if out is None:
        return "unknown"  # SSH failed / host unreachable
    # Look for "state=N" where N is 3 (PLAYING) or 8 (BUFFERING).
    for line in out.splitlines():
        if "PlaybackState" in line or "state=" in line:
            if "state=3" in line or "state=8" in line:
                return "playing"
    if "Permission Denial" in out or "not found" in out or not out.strip():
        return "unknown"
    return "stopped"


def pause_for_speech(host: str) -> bool:
    """Pause `host` for the duration of speech. Return True iff it should be
    auto-resumed afterwards.

    Auto-resume only when playback was positively confirmed (state=3/8),
    because `dispatch play` starts a session and resuming on a guess can
    spawn playback that was never there. When state is unknown we still
    pause defensively (a no-op if idle) but don't mark for resume unless
    MEDIA_ANDROID_RESUME_ON_UNKNOWN=1. With
    MEDIA_ANDROID_REQUIRE_PLAYING_DETECTION=1, unknown hosts are skipped
    entirely (no pause).

    Reads the state and acts on it in ONE ssh round-trip. Split across two
    commands this cost ~6s per sentence on a phone at 400ms RTT — the per-
    command overhead, not the work — and speech waited on all of it. The
    decision is cheap and pure, so it moves to the phone; we just report back
    which branch was taken.
    """
    state = _ssh(host, _pause_for_speech_script()) or "unknown"
    state = state.splitlines()[-1].strip() if state else "unknown"
    if state not in ("playing", "stopped", "unknown"):
        state = "unknown"

    if state == "playing":
        return True          # the script already dispatched pause
    if state == "stopped":
        return False         # confirmed idle — nothing paused, nothing to resume
    # unknown: the script paused defensively unless detection was required
    if require_detection():
        return False
    return resume_on_unknown()


def _pause_for_speech_script() -> str:
    """Classify playback and pause in one shot; echo the state we found.

    Mirrors `playback_state` + `pause_for_speech` policy, evaluated on the
    phone. `require_detection` is folded in so an unknown host is left alone
    when the operator asked for positive detection only.
    """
    skip_unknown = "1" if require_detection() else "0"
    return f"""
out=$(/system/bin/dumpsys media_session 2>&1)
case "$out" in
  *state=3*|*state=8*) st=playing ;;
  *"Permission Denial"*|*"not found"*|"") st=unknown ;;
  *) st=stopped ;;
esac
if [ "$st" = playing ] || {{ [ "$st" = unknown ] && [ "{skip_unknown}" = 0 ]; }}; then
  {pause_cmd()}
fi
echo "$st"
"""


def _pause_for_speech_legacy(host: str) -> bool:
    """The original two-round-trip form. Kept for reference/debugging."""
    state = playback_state(host)
    if state == "playing":
        pause(host)
        return True
    if state == "stopped":
        return False  # confirmed idle — don't touch it
    # state == "unknown"
    if require_detection():
        return False
    pause(host)
    return resume_on_unknown()


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
