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
import re
import subprocess

log = logging.getLogger(__name__)

# Chromium's rotating suffix, e.g. `chromium.instance12345`. Deliberately
# requires at least one digit so it cannot also swallow mpv-mpris's random
# `.instance-sLDnmIKJ`, which is a different process, not a re-registration.
_CHROMIUM_INSTANCE = re.compile(r"\.instance\d+$")

_TIMEOUT = 2.0
_SSH_CONNECT_TIMEOUT = 8        # seconds; sp4r SSH takes ~4.8s cold
_SSH_CMD_TIMEOUT = 12.0         # subprocess hard cap (connect + script)
_SSH_CONTROL_PERSIST = 300      # keep ControlMaster alive 5 min between clips

# Mopidy-MPRIS publishes `org.mpris.MediaPlayer2.mopidy` — lowercase, hardcoded
# in its server.py and not configurable. Matching is case-insensitive because
# this tuple read "Mopidy" for a long time and so never actually matched; the
# bug was invisible only because Mopidy-MPRIS wasn't installed anywhere yet.
_EXCLUDE_PREFIX = ("mopidy",)

# Our own mpv sinks expose MPRIS too, via mpv-mpris. They must never be paused
# for speech: pausing the speech broker stops the very clip we are making room
# for, and pausing music-mpv duplicates what the coordinator already does over
# MPD (which knows how to duck and resume; a blind MPRIS pause does not).
#
# They cannot be recognised by name. mpv-mpris takes `org.mpris.MediaPlayer2.mpv`
# and, when that is taken, appends a RANDOM suffix — measured on red5:
# `mpv.instance-sLDnmIKJ`. There is no way to set the bus name, and nothing in
# it distinguishes a sink from any other mpv on the box. So we go through the
# bus to the process: name -> owner PID -> /proc/<pid>/cmdline, which carries
# the IPC socket path and audio-client-name we launched it with.
_OWN_MPV_MARKERS = (
    "sink-speech.sock",         # speech broker (packages/core/services/sink-speech/run)
    "sink-book.sock",           # book broker (unit + sinks/book.py autospawn)
    "mopidy-mpv.sock",          # music, via mopidy-mpv.service
    "mopidy-mpv-music",         # ...and its audio-client-name
    "agent-media-book",         # book broker's audio-client-name
)


def own_mpv_markers() -> tuple[str, ...]:
    """Cmdline substrings identifying our own mpv instances.

    MEDIA_MPRIS_OWN_MARKERS=a,b ADDS markers rather than replacing them, so a
    host with an extra broker can protect it without having to restate (and
    risk dropping) the built-in set.
    """
    raw = os.environ.get("MEDIA_MPRIS_OWN_MARKERS", "")
    extra = tuple(m.strip() for m in raw.split(",") if m.strip())
    return _OWN_MPV_MARKERS + extra


def _is_mpv(name: str) -> bool:
    return name == "mpv" or name.startswith("mpv.")


def _bus_pid(name: str) -> int | None:
    """PID owning `org.mpris.MediaPlayer2.<name>`, or None if unknowable."""
    try:
        r = subprocess.run(
            ["busctl", "--user", "call",
             "org.freedesktop.DBus", "/org/freedesktop/DBus",
             "org.freedesktop.DBus", "GetConnectionUnixProcessID",
             "s", f"org.mpris.MediaPlayer2.{name}"],
            capture_output=True, text=True, timeout=_TIMEOUT)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0:
        return None
    # busctl prints the signature then the value: "u 1103981".
    parts = (r.stdout or "").split()
    if len(parts) != 2 or parts[0] != "u":
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def is_own_player(name: str) -> bool:
    """True if `name` is one of agent-media's own mpv instances.

    Fails CLOSED — an mpv we cannot identify is treated as ours and left
    alone. The two failure modes are not symmetric: mistaking our own broker
    for a stranger pauses speech mid-sentence (the pipeline's whole job),
    while mistaking a stranger for ours merely leaves some other audio playing
    under the clip. So when the bus, busctl or /proc won't answer, we decline
    to pause rather than risk silencing ourselves.
    """
    if not _is_mpv(name):
        return False
    pid = _bus_pid(name)
    if pid is None:
        return True
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmdline = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return True
    return any(m in cmdline for m in own_mpv_markers())


def _run(*args: str) -> str | None:
    try:
        r = subprocess.run(["playerctl", *args],
                           capture_output=True, text=True, timeout=_TIMEOUT)
        return (r.stdout or "").strip() if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


_SSH_OPTS = ["-o", "BatchMode=yes",
             "-o", f"ConnectTimeout={_SSH_CONNECT_TIMEOUT}",
             "-o", "ControlMaster=auto",
             "-o", "ControlPath=/tmp/ssh-am-%r@%h:%p",
             "-o", f"ControlPersist={_SSH_CONTROL_PERSIST}"]


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
            capture_output=True, text=True, timeout=_SSH_CMD_TIMEOUT,
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
    excluding Mopidy (handled separately via MPD) and our own mpv sinks.
    """
    out = _run("--list-all")
    if not out:
        return []
    result = []
    for name in out.splitlines():
        name = name.strip()
        if not name:
            continue
        if any(name.lower().startswith(ex) for ex in _EXCLUDE_PREFIX):
            continue
        if is_own_player(name):
            continue
        status = _run("--player", name, "status")
        if status == "Playing":
            result.append(name)
    return result


def remote_playing_players(host: str) -> list[str]:
    """Return names of Playing MPRIS players on a remote host (one SSH call).

    Mirrors the local filter, own-mpv guard included — a remote host running
    agent-media has brokers of its own, and pausing those over SSH is exactly
    as wrong as pausing the local ones.
    """
    exclude = " ".join(f'"{p}"' for p in _EXCLUDE_PREFIX)
    markers = " ".join(f'"{m}"' for m in own_mpv_markers())
    script = f"""
exclude=({exclude})
markers=({markers})
for p in $(playerctl --list-all 2>/dev/null); do
    skip=0
    lower=$(printf '%s' "$p" | tr '[:upper:]' '[:lower:]')
    for ex in "${{exclude[@]}}"; do [[ "$lower" == "$ex"* ]] && skip=1 && break; done
    [ $skip -eq 1 ] && continue
    # Our own mpv sinks: identify via bus owner PID -> cmdline, and fail CLOSED
    # (an mpv we cannot identify is left alone) for the same reason as locally.
    if [[ "$p" == "mpv" || "$p" == mpv.* ]]; then
        pid=$(busctl --user call org.freedesktop.DBus /org/freedesktop/DBus \
            org.freedesktop.DBus GetConnectionUnixProcessID \
            s "org.mpris.MediaPlayer2.$p" 2>/dev/null | awk '$1=="u"{{print $2}}')
        [ -z "$pid" ] && continue
        cmd=$(tr '\\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
        [ -z "$cmd" ] && continue
        own=0
        for m in "${{markers[@]}}"; do [[ "$cmd" == *"$m"* ]] && own=1 && break; done
        [ $own -eq 1 ] && continue
    fi
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
        # At least one digit: Chromium re-registers as .instanceNNN, whereas
        # mpv-mpris's .instance-<random> is a different process entirely.
        base=$(echo "$name" | sed -E 's/\\.instance[0-9]+$//')
        [ "$base" = "$name" ] && continue
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

    Only NUMERIC instance suffixes are collapsed, because only Chromium's are
    numeric and re-registered by the same player. mpv-mpris suffixes are random
    (`mpv.instance-sLDnmIKJ`) and belong to genuinely distinct processes, so
    prefix-matching them would resume some unrelated mpv instead — it must be
    an exact match or nothing.
    """
    base = _CHROMIUM_INSTANCE.sub("", name)
    if base == name:
        return name if name in current else None
    return next((n for n in current if n == base or n.startswith(base + ".")),
                None)
