"""media — unified CLI control surface for agent-media.

Speech playback control (sink-speech), the now-speaking text + history
(state/), and music control (sink-music / Mopidy). The tmux popup, status
line, and keybind plugin all drive this CLI — it replaces the old aar
`tts-ctl` / `tts-popup` / `tts-status-line` shell bins (decision 5).

Speech control always targets the local sink-speech broker (the thing
producing audio on this host); routing of *new* clips to rooms/etc. is a
submit-time concern (see intake/submit).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from typing import Optional

from ._paths import state_dir
from .sinks import _mpv_ipc as ipc
from .sinks.music import SinkMusic
from .sinks.speech import SinkSpeech, _socket_for
from .state import StateStore
from .types import Event, Priority, Source, Target

POPUP_CHANNELS = ("speech", "music", "book")

# Load machine-local config (~/.config/agent-media.env) so the CLI — including
# the tmux status bar and the popup controls — resolves the same speech target
# the hook plays to. Without this the status/popup would read the *local* mpv
# even when speech is playing on a remote target (the phone, Grade B), and show
# nothing. Real env vars still win, matching the hooks' precedence.
try:
    from .intake._env import load_env_file as _load_env_file
    _load_env_file("cli")
except Exception:  # noqa: BLE001 — config is best-effort; CLI must still run
    pass

# The speech target the control surface reads/drives. For a remote target (the
# phone over a tcp:// bridge) media status/now/pause/skip/replay all talk to
# *that* mpv, so the popup follows phone-local playback (Grade B). Falls back to
# the local broker when unset.
SPEECH_TARGET = Target(os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET", "local"))


# --- pure helpers (unit-tested) -------------------------------------------

def fmt_mmss(secs: Optional[float]) -> str:
    if secs is None:
        return "--:--"
    secs = max(0, int(secs))
    return f"{secs // 60:02d}:{secs % 60:02d}"


def fmt_time(secs: Optional[float], *, hours: Optional[bool] = None) -> str:
    """Compact duration. `hours=True` → ``H:MM`` (audiobook scale — minutes,
    no seconds); `hours=False` → ``M:SS``; `None` auto-picks ``H:MM`` once the
    value reaches an hour. Pass an explicit `hours` (derived from the *total*)
    so a pos/total pair shares one format — otherwise a 45-min position into an
    11-hour book would render ``45:00`` next to ``11:05``.

    Keeps long content compact: an 11h book is ``11:05`` instead of fmt_mmss's
    overflowing ``665:37``.
    """
    if secs is None:
        return "--:--"
    secs = max(0, int(secs))
    if hours is None:
        hours = secs >= 3600
    if hours:
        h, rem = divmod(secs, 3600)
        return f"{h}:{rem // 60:02d}"
    # Sub-hour stays byte-identical to fmt_mmss (MM:SS) so speech/music status
    # is unchanged; only >= 1h content switches to the compact H:MM above.
    return f"{secs // 60:02d}:{secs % 60:02d}"


def progress_bar(frac: float, width: int = 12) -> str:
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    return "█" * filled + "░" * (width - filled)


def render_status(*, idle: Optional[bool], pos: Optional[float],
                  dur: Optional[float], paused: Optional[bool],
                  muted: Optional[bool], width: int = 12,
                  hide_idle: bool = True, bar: bool = True,
                  speed: Optional[float] = None) -> str:
    """Build the one-line status string (or '' / '○' when idle).

    With bar=False, the progress bar is dropped and only the times remain
    (`▶ 00:30 / 02:00`) — used by the popup, which shows just the clock.
    `speed` (when not ~1.0) appends a `⏩1.4×` readout so a listening-mode
    speed change is visible in the status bar.
    """
    if idle is None or idle:
        return "" if hide_idle else "○"
    icon = "⏸" if paused else "▶"
    # Format chosen by the total's magnitude (applied to both) so an 11h book
    # reads `1:55 / 11:05` rather than the overflowing `115:32 / 665:37`.
    hours = bool(dur is not None and dur >= 3600)
    if bar:
        frac = (pos / dur) if (pos and dur) else 0.0
        line = (f"{icon} {fmt_time(pos, hours=hours)} {progress_bar(frac, width)} "
                f"{fmt_time(dur, hours=hours)}")
    else:
        line = f"{icon} {fmt_time(pos, hours=hours)} / {fmt_time(dur, hours=hours)}"
    if muted:
        line += " [M]"
    if isinstance(speed, (int, float)) and abs(speed - 1.0) > 0.05:
        glyph = "⏩" if speed > 1.0 else "🐢"
        line += f" {glyph}{speed:.2g}×"
    return line


# --- IPC plumbing ----------------------------------------------------------

def _active_speech_target() -> Target:
    """The target speech is *actually* playing on — the now-playing mirror's
    recorded target, falling back to the configured default when idle.

    The daemon that started playback resolves its target from its own env
    (`MEDIA_SPEECH_DEFAULT_TARGET`, e.g. the phone), but a popup keypress spawns
    a short-lived `media` in the user's shell, which usually lacks that var and
    so would default to `local`. Reading the wrong player makes the status show
    `○` and pause act on an empty local mpv. Follow the live player instead —
    the same precedence the nav/skip path already uses (now-playing target, then
    SPEECH_TARGET). When idle there's no row, so this is just SPEECH_TARGET.
    """
    name = (StateStore().get_now_playing("speech") or {}).get("target")
    return Target(name=name) if name else SPEECH_TARGET


def _sock():
    return _socket_for(_active_speech_target())


def _get(prop: str):
    try:
        return ipc.get_property(_sock(), prop)
    except ipc.MpvIpcError:
        return None


def _now_speaking() -> Optional[dict]:
    np = StateStore().get_now_playing("speech")
    if not np:
        return None
    ex = np.get("extras") or {}
    if isinstance(ex, str):
        try:
            ex = json.loads(ex)
        except json.JSONDecodeError:
            ex = {}
    np["extras"] = ex
    return np


def _speech_history(n: int = 20, session: Optional[str] = None):
    # `session`, when given, is a *Claude session id* (extras.source_session) —
    # the true "this conversation" boundary. It's preferred over the tmux
    # session because one tmux session can hold several distinct conversations
    # (so tmux-scoping bleeds between them) and one conversation can move panes
    # on resume (so pane-scoping splits it). The Claude id does neither.
    #
    # Exclude "Claude is waiting" notif clips: they're alerts, not responses,
    # and shouldn't appear when traversing past TTS (popup < / >, r, replay).
    # Over-fetch so filtering still leaves n real responses to step through;
    # over-fetch harder when scoping, since other conversations' clips
    # interleave and would otherwise crowd out the buffer.
    fetch = max(n * 4, n + 50)
    if session:
        fetch = max(fetch, 400)
    rows = StateStore().recent_history(sink="speech", limit=fetch)
    rows = [r for r in rows
            if not (isinstance(r.get("extras"), dict)
                    and r["extras"].get("kind") == "notif")]
    if session:
        # Scope traversal to one conversation's clips. Rows that predate the
        # source_session field (or came from a session-less source) carry no
        # tag and are excluded rather than leaking across conversations.
        rows = [r for r in rows
                if isinstance(r.get("extras"), dict)
                and r["extras"].get("source_session") == session]
    return rows[:n]


def _tmux_session_for_pane(pane: str) -> str:
    """Resolve a tmux pane id (e.g. ``%41``) to its session name, or ""."""
    if not pane or "#{" in pane:
        return ""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_name}"],
            capture_output=True, text=True, timeout=2)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _anchor_session() -> Optional[str]:
    """The *conversation* the popup's < / > traversal should stay within,
    as a Claude session id (extras.source_session).

    Follows what you're hearing: the now-playing clip's conversation if one is
    playing; otherwise the conversation that last spoke in the pane that opened
    the popup (TTS_POPUP_PANE). The Claude id is the right scope — it survives a
    session being resumed into another pane and doesn't bleed across sibling
    conversations sharing one tmux session. Returns None when neither resolves,
    so callers fall back to unscoped (all-conversation) history.
    """
    np = StateStore().get_now_playing("speech")
    ex = (np or {}).get("extras") or {}
    if isinstance(ex, str):
        try:
            ex = json.loads(ex)
        except json.JSONDecodeError:
            ex = {}
    sess = ex.get("source_session")
    if sess:
        return sess
    # Idle: a bare pane id carries no Claude id, so resolve it from that pane's
    # most recent clip (most-recent-first history).
    pane = os.environ.get("TTS_POPUP_PANE", "")
    if pane:
        for r in _speech_history(50):
            rex = r.get("extras") or {}
            if rex.get("source_pane") == pane and rex.get("source_session"):
                return rex["source_session"]
    return None


def _caller_pane() -> str:
    """The pane the user is "at", for the different-pane (↪) comparison.

    Popup: TTS_POPUP_PANE (TMUX_PANE inside display-popup is the popup's own
    ephemeral pane). Status bar: MEDIA_STATUS_PANE, which the status-right
    config passes as `#{pane_id}` (the viewing client's active pane) — without
    it the status bar has no pane context, so the ↪ comparison can't tell which
    pane you're on. Falls back to TMUX_PANE. An unexpanded `#{...}` literal is
    resolved by asking tmux for the active pane."""
    pane = (os.environ.get("TTS_POPUP_PANE")
            or os.environ.get("MEDIA_STATUS_PANE")
            or os.environ.get("TMUX_PANE", ""))
    if "#{" in pane:
        try:
            r = subprocess.run(["tmux", "display-message", "-p", "#{pane_id}"],
                               capture_output=True, text=True)
            pane = r.stdout.strip() if r.returncode == 0 else ""
        except Exception:  # noqa: BLE001
            pane = ""
    return pane


# --- speech subcommands ----------------------------------------------------

def _remote_speech() -> bool:
    """Speech plays on a remote target (the phone, over a tcp:// bridge)."""
    return str(_sock()).startswith("tcp://")


def _speech_display_state():
    """`(idle, pos, dur, paused, muted, speed, playing)` for the speech channel.

    Remote target (the phone): read the intake monitor's local now_playing
    mirror — a DB hit, not a ~600ms bridge round-trip — so the status bar and
    popup stay responsive. Local target: one batched snapshot off the local mpv,
    enriched with the response timeline (offset+pos / total) from now_playing.
    """
    if _remote_speech():
        np = _now_speaking()
        ex = (np or {}).get("extras") if np else None
        if ex and ex.get("total_duration_s"):
            # Zombie guard: the mirror is only as alive as the submit process
            # that writes it. If that process died without its cleanup (kill,
            # crash, power loss), the row would otherwise show a frozen
            # "▶ 00:00 / N:NN" forever. Both playback paths stamp their pid.
            wp = ex.get("writer_pid")
            if wp:
                try:
                    os.kill(int(wp), 0)
                except (OSError, ValueError):
                    return (True, None, None, False, False, None, False)
            lp = ex.get("live_pos_s")
            pos = lp if lp is not None else (ex.get("clip_offset_s") or 0.0)
            return (False, pos, ex.get("total_duration_s"),
                    bool(ex.get("live_pause")), bool(ex.get("live_mute")),
                    ex.get("live_speed") or 1.0, True)
        return (True, None, None, False, False, None, False)
    try:
        snap = ipc.get_properties(
            _sock(), ["idle-active", "time-pos", "duration", "pause", "mute",
                      "speed", "playlist-pos"])
    except Exception:  # noqa: BLE001
        snap = {}
    idle = snap.get("idle-active")
    pos = snap.get("time-pos")
    dur = snap.get("duration")
    playing = False         # the response timeline (offset+pos / total) is known
    if not idle:
        np = _now_speaking()
        if np:
            ex = np.get("extras") or {}
            total = ex.get("total_duration_s")
            if total:
                clip_durs = ex.get("clip_durations_s")
                if clip_durs:
                    # Replay path: offset from playlist-pos (from the snapshot).
                    ppos = max(0, int(snap.get("playlist-pos") or 0))
                    offset = sum(clip_durs[:ppos])
                else:
                    offset = ex.get("clip_offset_s") or 0.0
                pos = offset + (pos or 0.0)
                dur = total
                playing = True
    return (idle, pos, dur, snap.get("pause"), snap.get("mute"),
            snap.get("speed"), playing)


def _speech_visual_flag() -> str:
    """"figure"/"reveal" while the now-playing speech message carries a
    purposeful visual ([[visual:]]/[[reveal:]] marker), else "". Drives the
    ▣ indicator in the status bar and popup — never load-bearing, so any
    lookup problem is just "no indicator"."""
    try:
        np = _now_speaking()
        return ((np or {}).get("extras") or {}).get("visual") or ""
    except Exception:  # noqa: BLE001
        return ""


def _with_visual_glyph(line: str) -> str:
    """Append the figure indicator to a rendered status line: the listener's
    cue that this spoken message has a picture worth looking at."""
    if line and not line.startswith("○") and _speech_visual_flag():
        return f"{line} ▣"
    return line


def _skew_alert_line() -> str:
    """Read the version skew ledger produced by `media doctor`. Flashing `⚠`
    shown in the status bar if any host is running stale agent-media/dotfiles code.
    Auto-triggers a background check every 2 hours."""
    try:
        from pathlib import Path
        import stat
        d = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
        logdir = d / "agent-media"
        logdir.mkdir(parents=True, exist_ok=True)
        ledger = logdir / "version-skew.log"
        
        # Async check every 2h
        try:
            mtime = ledger.stat().st_mtime
        except FileNotFoundError:
            mtime = 0
            
        if time.time() - mtime > 7200:
            ledger.touch() # prevent concurrent spawns
            import subprocess
            subprocess.Popen(
                [sys.argv[0], "doctor"],
                start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            
        skew = ledger.read_text().strip()
        if not skew:
            return ""
        glyph = "⚠" if int(time.time()) % 2 else " "
        return f"{glyph} skew: {skew.replace(chr(10), ', ')}"
    except OSError:
        return ""


def _miss_alert_line() -> str:
    """Flashing `⚠ <target> unreachable (N lost)` — shown INSTEAD of the
    progress bar while spoken replies are known lost and the target hasn't
    acknowledged (the miss ledger is pending). A frozen `▶ 00:00` bar reads
    as playback; a lost reply must read as a fault. The status bar redraws
    ~1/s, so alternating the glyph on the epoch second makes it blink."""
    try:
        from .sinks._miss_notify import pending_miss
        pm = pending_miss()
    except Exception:  # noqa: BLE001 — an alert lookup must never break status
        return ""
    if not pm:
        return ""
    count, _latest = pm
    glyph = "⚠" if int(time.time()) % 2 else " "
    return f"{glyph} {_active_speech_target().name} unreachable ({count} lost)"


def cmd_status(a) -> int:
    alert = _skew_alert_line() or _miss_alert_line()
    if alert:
        print(alert)
        return 0
    idle, pos, dur, paused, muted, speed, playing = _speech_display_state()
    # Optional title-overlay bar (EXPERIMENTAL): the whole `▶ pos title dur`
    # segment becomes one background-progress bar, times embedded in the fill.
    # `--title` carries the tmux client width; the title-field width is derived
    # from it (_title_window) so one config fits any screen. Only while playing.
    cw = getattr(a, "title", None)
    if cw and playing:
        prefix, body = _subject_label()
        if prefix or body:
            print(_title_status_line(pos, dur, paused, muted, speed, prefix,
                                     body, _title_window(cw), key="status"))
            return 0
    print(_with_visual_glyph(
        render_status(idle=idle, pos=pos, dur=dur, paused=paused, muted=muted,
                      width=a.width, hide_idle=not a.show_idle,
                      bar=not getattr(a, "no_bar", False), speed=speed)))
    return 0


def cmd_popup_status(a) -> int:
    """Aggregate the speech popup's whole redraw into ONE process: three lines —
    status / subject-pane label / durable-mute count. The popup used to spawn
    `status` + `now-pane` + `mute-count` separately (~3× Python startup) on every
    refresh, which made it slow to open and slow to react. Emits exactly three
    newline-terminated fields (any may be empty) so the caller reads them with
    three `read -r`s.

    With ``--act VERB [ARGS…]`` it first runs that media subcommand *in this
    process* (reusing main()'s parser/dispatch) and prepends its stdout,
    whitespace-collapsed, as a leading line — so a popup keypress costs ONE
    `media` spawn (action + redraw) instead of two. The popup keeps the
    key→verb map (one source of truth); this just fuses the two spawns. That
    leading line carries e.g. `replay-prev`'s resolved history cursor back to
    the popup; it's empty for actions that print nothing.
    """
    act = getattr(a, "act", None)
    if act:
        import contextlib
        import io
        buf = io.StringIO()
        try:
            ns = _build_parser().parse_args(_end_opts_before_time(act))
            with contextlib.redirect_stdout(buf):
                ns.func(ns)
        except SystemExit:
            pass          # a malformed/parse-failed action must not eat the redraw
        except Exception:  # noqa: BLE001 — nor may an action error blank the popup
            pass
        # Leading line = the action's own output (collapsed to one line), which
        # the caller reads before the three status fields.
        print(" ".join(buf.getvalue().split()))
    alert = _miss_alert_line()
    if alert:
        print(alert)
    else:
        idle, pos, dur, paused, muted, speed, _ = _speech_display_state()
        print(_with_visual_glyph(
            render_status(idle=idle, pos=pos, dur=dur, paused=paused,
                          muted=muted, width=a.width,
                          hide_idle=not a.show_idle,
                          bar=not getattr(a, "no_bar", False), speed=speed)))
    prefix, body = _subject_label()
    print(f"{prefix}{body}" if (prefix or body) else "")
    m = StateStore().list_mutes()
    n = sum(1 for v in m["panes"].values() if v) + \
        sum(1 for v in m["sessions"].values() if v)
    print(n if n else "")
    return 0


def cmd_now(a) -> int:
    np = _now_speaking()
    if np:
        print((np["extras"].get("text") or "").strip())
    return 0


def _spoken_extras() -> dict:
    """Extras of the current (or most recent) speech — source of pane/session.

    Actively playing: THIS clip's extras (or {} when paneless — a gateway/
    openclaw agent, `media say`, etc.). Don't fall back to history while
    playing, or paneless speech would borrow the last Claude pane.
    Idle: the most recent clip's extras, so we keep naming whoever last spoke.
    """
    np = _now_speaking()
    if np:
        return np.get("extras") or {}
    rows = _speech_history(1)
    if rows:
        ex = rows[0].get("extras") or {}
        if isinstance(ex, str):
            try:
                ex = json.loads(ex)
            except json.JSONDecodeError:
                ex = {}
        return ex
    return {}


def _spoken_pane() -> Optional[str]:
    """tmux pane id that produced the current (or most recent) speech."""
    return _spoken_extras().get("source_pane") or None


def _spoken_session() -> Optional[str]:
    """Claude Code session id behind the current (or most recent) speech.

    Captured at speech time by the hook, so it survives the source pane being
    closed — lets `goto-pane` offer to resume the conversation.
    """
    return _spoken_extras().get("source_session") or None


def _subject() -> tuple[str, str, bool]:
    """The single thing the popup acts on: ``(pane, tmux_session, following)``.

    "What you see is what every key acts on." The subject is whatever is
    *playing now* (the pane you're actually hearing), else the pane that opened
    the popup. The title, the `M` key, the 🔒 indicator and the `<`/`>` scope
    all resolve through this, so they never disagree. `following` is True when
    the subject is a *different* pane than the caller — i.e. you're hearing
    another conversation, not your own — which the popup flags with `↪`.

    Uses *now-playing* only (not last-history) as the active signal, so an idle
    popup is always "about your pane", never a stale background speaker.
    """
    np = StateStore().get_now_playing("speech")
    ex = (np or {}).get("extras") or {}
    if isinstance(ex, str):
        try:
            ex = json.loads(ex)
        except json.JSONDecodeError:
            ex = {}
    caller = _caller_pane()
    np_pane = ex.get("source_pane") or ""
    if np_pane:
        # "following" (↪) only when we actually have a caller pane to compare
        # AND the subject pane is a live pane *on this server*: the status bar
        # runs `media status` with no pane context (caller=""), where we can't
        # tell; and a pane that's dead here — renumbered by a tmux restore,
        # closed, or living on another host (rooms hub) — isn't a "different
        # live pane" we can honestly point at, so don't flag it with ↪.
        following = bool(caller) and _pane_alive(np_pane) and np_pane != caller
        return np_pane, ex.get("source_tmux_session") or "", following
    return caller, (_tmux_session_for_pane(caller) if caller else ""), False


def _focus_pane(pane: str) -> None:
    """Bring `pane` to the foreground for the calling client.

    Selects the pane within its window/session, then `switch-client`s the
    calling client to that pane's session — without the last step, focus
    never follows when the pane lives in a *different* session than the one
    the popup was opened from (select-window/select-pane only move the target
    session's active pane, not the attached client). Each step is best-effort
    so a missing client or a since-closed pane can't surface a traceback.
    """
    for args in (["select-window", "-t", pane], ["select-pane", "-t", pane]):
        try:
            subprocess.run(["tmux", *args], capture_output=True)
        except Exception:  # noqa: BLE001
            pass
    # Resolve the pane's session and switch the client there.
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_name}"],
            capture_output=True, text=True)
        sess = (r.stdout or "").strip()
        if sess:
            subprocess.run(["tmux", "switch-client", "-t", sess],
                           capture_output=True)
    except Exception:  # noqa: BLE001
        pass


def _pane_alive(pane: str) -> bool:
    """True if `pane` is still an open tmux pane on this server."""
    if not pane:
        return False
    try:
        r = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
            capture_output=True, text=True)
    except Exception:  # noqa: BLE001
        return False
    if r.returncode != 0:
        return False
    return pane in r.stdout.split()


def _subject_label() -> "tuple[str, str]":
    """`(prefix, title)` for the subject pane — what every key acts on.

    Names the pane playing now, or (idle) the pane that opened the popup, via
    `_subject()`. `prefix` holds the leading indicators — `↪ ` when the subject
    is a *different* pane than the caller (you're hearing another conversation)
    and `🔒 ` when that subject is muted — returned *separately* from the title
    so a marquee can pin them (keep them fixed) while only the title scrolls.
    `('', '')` when no subject pane resolves.

    Prefers the *window name* (which tracks the stable Claude conversation
    title) over the *pane title* — the pane title is the transient tool-status,
    so it carries a leading spinner glyph and flips to whatever Claude is doing
    right now rather than naming what's actually being spoken. Falls back to a
    spinner-stripped pane title only when the window has no usable name.

    Shared by `now-pane` (popup marquee) and the optional status-bar marquee.
    """
    pane, tmux_sess, following = _subject()
    if not pane:
        return "", ""
    # Resolve the live pane name only when the pane is actually open on this
    # server. A pane that's dead here (renumbered by a tmux-resurrect restore,
    # closed since, or — for a rooms hub — living on another host) returns
    # success-with-empty-fields from `display-message`, which the old
    # `returncode != 0` guard sailed straight past, leaving a blank title.
    label = ""
    if _pane_alive(pane):
        try:
            r = subprocess.run(
                ["tmux", "display-message", "-p", "-t", pane,
                 "#{window_name}\t#{pane_title}"],
                capture_output=True, text=True)
        except Exception:  # noqa: BLE001 — popup must never see a traceback
            r = None
        if r is not None and r.returncode == 0:
            window_name, _, pane_title = r.stdout.strip().partition("\t")
            # A default-named window (the shell/program name) is no better than
            # the pane title; only prefer it when it's a real conversation title.
            label = window_name.strip()
            if not label or label in {"zsh", "bash", "sh", "fish"}:
                # Strip a leading Claude spinner glyph (braille U+2800–U+28FF).
                label = re.sub(r"^[⠀-⣿]\s*", "", pane_title.strip())
    if not label:
        # Pane unresolvable here — fall back to the conversation title captured
        # at speech time and carried in the speech extras (source_window). This
        # is what lets the bar name a renumbered/closed/remote speaker instead
        # of showing a bare ↪ with an empty title.
        label = (_spoken_extras().get("source_window") or "").strip()
    prefix = ""
    if following:
        prefix += "↪ "
    if StateStore().resolve_mute(pane, tmux_sess):
        prefix += "🔒 "
    return prefix, label


def _marquee(text: str, width: int, *, key: str = "status",
             gap: str = "   ") -> str:
    """A `width`-column window into `text`, scrolling one column per call.

    The tmux status bar redraws at most once a second (status-interval floor),
    so this advances 1 col/call — a coarse crawl, not the popup's smooth glide.
    Offset persists in a state file (this runs as a fresh process each refresh)
    and resets when `text` changes. Text that already fits is returned as-is.
    """
    text = " ".join(text.split())
    if not text or width <= 0:
        return ""
    if len(text) <= width:
        return text
    p = state_dir() / f"marquee-{key}"
    try:
        saved = json.loads(p.read_text())
        last, off = saved.get("t"), int(saved.get("o", 0))
    except Exception:  # noqa: BLE001
        last, off = None, 0
    if last != text:
        off = 0
    loop = text + gap
    window = (loop + loop)[off:off + width]
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"t": text, "o": (off + 1) % len(loop)}))
    except OSError:
        pass
    return window


def _client_width(v) -> int:
    """argparse type for --title: a tmux client width, tolerant of a literal
    unexpanded `#{client_width}` (→ 80) so the status bar never errors out."""
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return 80


def _title_window(client_width: int) -> int:
    """Title-field width derived from the tmux client width, so one status-bar
    config fits any screen (wide desktop → roomy, ~32-col phone → tight): a
    quarter of the client, clamped. Bounds via MEDIA_STATUS_TITLE_{MIN,MAX}."""
    tmin = int(os.environ.get("MEDIA_STATUS_TITLE_MIN", "8"))
    tmax = int(os.environ.get("MEDIA_STATUS_TITLE_MAX", "26"))
    return max(tmin, min(tmax, client_width // 4))


def _title_status_line(pos: Optional[float], dur: Optional[float],
                       paused: Optional[bool], muted: Optional[bool],
                       speed: Optional[float], prefix: str, body: str,
                       width: int, *, key: str = "status") -> str:
    """The whole speech status segment as ONE background-progress bar.

    `▶ {pos} {prefix}{scrolling title} {dur}` is rendered as a single field
    whose background colour-fills left→right by progress, so the numeric times
    on either side of the title are *part of* the bar rather than sitting
    outside it. `prefix` (the ↪/🔒 indicators) is pinned — only `body` scrolls
    within the remaining space — so the indicator stays visible. `width` is the
    title field (prefix + body window); the times/icon add a few cols either
    side.

    Emits tmux `#[...]` directives (honoured inside `#()` status output).
    Colours via MEDIA_STATUS_TITLE_{FILL,REST}; `#[default]` resets at the end
    (set the env vars to the theme's status-right colours if the handoff to
    whatever follows looks off). Mute/speed readouts ride after the bar.
    """
    icon = "⏸" if paused else "▶"
    hours = bool(dur is not None and dur >= 3600)
    bodywin = _marquee(body, max(1, width - len(prefix)), key=key)
    titlefield = f"{prefix}{bodywin}"
    inner = f"{icon} {fmt_time(pos, hours=hours)} {titlefield} {fmt_time(dur, hours=hours)}"
    frac = (pos / dur) if (pos and dur) else 0.0
    frac = max(0.0, min(1.0, frac))
    split = int(round(frac * len(inner)))
    fill = os.environ.get("MEDIA_STATUS_TITLE_FILL", "bg=colour24,fg=colour231")
    rest = os.environ.get("MEDIA_STATUS_TITLE_REST", "bg=colour236,fg=colour250")
    line = f"#[{fill}]{inner[:split]}#[{rest}]{inner[split:]}#[default]"
    if muted:
        line += " [M]"
    if isinstance(speed, (int, float)) and abs(speed - 1.0) > 0.05:
        line += f" {'⏩' if speed > 1.0 else '🐢'}{speed:.2g}×"
    return line


def cmd_now_pane(a) -> int:
    """Print the popup's subject-pane title (see `_subject_label`).

    With `--width N` the body is windowed through `_marquee` (own state key,
    so it doesn't double-advance the status bar's crawl) — used by the control
    popup's border title, re-expanded by tmux once per status-interval.
    """
    if getattr(a, "session_only", False):
        pane, sess, _following = _subject()
        print(sess or (_tmux_session_for_pane(pane) if pane else ""))
        return 0
    prefix, body = _subject_label()
    width = getattr(a, "width", None)
    if width:
        body = _marquee(body, max(1, width - len(prefix)), key="popup-border")
    if prefix or body:
        print(f"{prefix}{body}")
    return 0


def cmd_goto_pane(a) -> int:
    """Focus the pane that produced the now-playing (or last) speech.

    Exit codes let the popup react instead of silently no-opping when the
    pane is gone:
      0  focused a live pane (nothing printed)
      3  pane is closed but a Claude session is resumable — its id is printed
         on stdout so the popup can offer `claude --resume <id>`
      2  pane is closed and there's nothing to resume
      1  no source pane was ever captured (paneless speech)
    """
    pane = _spoken_pane()
    if pane and _pane_alive(pane):
        _focus_pane(pane)
        return 0
    session = _spoken_session()
    if session:
        print(session)
        return 3
    if pane:
        return 2  # had a pane, it's closed, no session to fall back to
    return 1


def _session_cwd(sid: str) -> Optional[str]:
    """Working directory a Claude Code session was recorded under, or None.

    `claude --resume <id>` only finds a session when run from that session's
    project directory — transcripts live under ~/.claude/projects/<enc-cwd>/,
    keyed by the cwd they ran in. So a resume launched from the wrong pane's
    cwd fails with "No conversation found". Recover the real cwd from the
    transcript's first line that carries one.
    """
    import glob as _glob
    root = os.path.expanduser("~/.claude/projects")
    hits = _glob.glob(os.path.join(root, "*", f"{sid}.jsonl"))
    if not hits:
        return None
    try:
        with open(hits[0], encoding="utf8", errors="replace") as fh:
            for line in fh:
                if '"cwd"' not in line:
                    continue
                try:
                    cwd = json.loads(line).get("cwd") or ""
                except Exception:  # noqa: BLE001
                    continue
                if cwd:
                    return cwd
    except Exception:  # noqa: BLE001
        return None
    return None


def cmd_open_session(a) -> int:
    """Open a new tmux window resuming the given Claude Code session.

    The popup calls this after `goto-pane` reports a closed pane (rc 3) and
    the user confirms — it brings the conversation back as `claude --resume`.

    The new window MUST start in the session's own project cwd: `claude
    --resume` resolves the id per-project, so launching from the caller pane's
    directory (whatever it happened to be) would fail silently and the window
    would close instantly. Mirror the claude-resume CLI: `-c <cwd>` plus
    `env -u ANTHROPIC_API_KEY` so a stray key doesn't override the login.
    """
    sid = (getattr(a, "session", "") or "").strip()
    if not sid:
        return 1
    argv = ["tmux", "new-window"]
    cwd = _session_cwd(sid)
    if cwd:
        argv += ["-c", cwd]
    argv.append(f"env -u ANTHROPIC_API_KEY claude --resume {sid}")
    try:
        subprocess.run(argv, capture_output=True)
    except Exception:  # noqa: BLE001
        return 1
    return 0


def _ncmpcpp_pane() -> Optional[str]:
    """tmux pane id running ncmpcpp on this server, or None.

    Scans every pane (all sessions/windows) and matches the foreground
    command, so the music `g` lands on the player wherever it lives.
    """
    try:
        r = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{pane_id}\t#{pane_current_command}"],
            capture_output=True, text=True)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        pane, _, cmd = line.partition("\t")
        if cmd.strip() == "ncmpcpp":
            return pane
    return None


def cmd_goto_track(a) -> int:
    """Focus the ncmpcpp pane and jump it to the now-playing song.

    Mirrors the speech side's goto-pane for the music channel: bring the
    player to the foreground, then send ncmpcpp's default JumpToPlayingSong
    key (`o`) so it centers on the track the music sink is playing. Returns
    1 (and stays quiet) when no ncmpcpp pane is running, so the popup can
    show a hint instead of silently doing nothing.
    """
    pane = _ncmpcpp_pane()
    if not pane:
        return 1
    _focus_pane(pane)
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "o"],
                       capture_output=True)
    except Exception:  # noqa: BLE001
        pass
    return 0


def cmd_open_ncmpcpp(a) -> int:
    """Open a new tmux window running ncmpcpp.

    The popup calls this when the music `g` found no ncmpcpp pane and the
    user confirms. ncmpcpp's config sets `jump_to_now_playing_song_at_start`,
    so it lands on the current track without us sending `o`. The launch
    command is overridable via MEDIA_NCMPCPP_CMD (e.g. a wrapper or a path).
    """
    cmd = os.environ.get("MEDIA_NCMPCPP_CMD", "ncmpcpp")
    try:
        subprocess.run(["tmux", "new-window", cmd], capture_output=True)
    except Exception:  # noqa: BLE001
        return 1
    return 0


# Window name we launch the book's mpvc-tui under, so goto-book can find it
# again regardless of what the foreground command reports (rlwrap/sh/mpvc-tui).
_MPVC_WINDOW = "agent-media-book"


def _mpvc_pane() -> Optional[str]:
    """tmux pane id showing the book's mpvc-tui, or None.

    The book channel's player is mpvc-tui — an IPC client of the headless
    sink-book broker (analogous to ncmpcpp for Mopidy). We launch it in a
    window named `_MPVC_WINDOW`, so match that first; also accept a pane whose
    foreground command is mpvc-tui in case one was started by hand.
    """
    try:
        r = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{pane_id}\t#{window_name}\t#{pane_current_command}"],
            capture_output=True, text=True)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pane, wname, cmd = parts
        if wname == _MPVC_WINDOW or cmd.strip() == "mpvc-tui":
            return pane
    return None


def cmd_goto_book(a) -> int:
    """Focus the book's mpvc-tui player pane (the book channel's `g`).

    Mirrors goto-track for the book channel: bring an existing mpvc-tui to the
    foreground. Since mpvc-tui only drives the broker over IPC, the audiobook
    keeps playing on the multi-room stream. Returns 1 (quietly) when no
    mpvc-tui is running, so the popup can offer to open one.
    """
    pane = _mpvc_pane()
    if not pane:
        return 1
    _focus_pane(pane)
    return 0


def cmd_open_mpvc(a) -> int:
    """Open a new tmux window running mpvc-tui bound to the book socket.

    The popup calls this when the book `g` found no mpvc-tui pane and the user
    confirms. mpvc-tui's socket already defaults to sink-book.sock; the launch
    command/mode is overridable via MEDIA_MPVC_CMD (e.g. `mpvc-tui -tt` for the
    tiny TUI, or a wrapper that sets the socket explicitly).
    """
    cmd = os.environ.get("MEDIA_MPVC_CMD", "mpvc-tui -t")
    try:
        subprocess.run(["tmux", "new-window", "-n", _MPVC_WINDOW, cmd],
                       capture_output=True)
    except Exception:  # noqa: BLE001
        return 1
    return 0


def _ask_context(channel: str) -> str:
    """A one-line "what I'm listening to" blurb for the active channel.

    Recombines what the popup already surfaces per channel into a compact,
    paste-ready line so `media open-pi` can seed a fresh pi/nvim session with
    the user's current place in their listening. Never raises — a broken
    backend just yields an empty (or partial) blurb rather than a traceback in
    the popup's `a` path.
    """
    ch = (channel or "speech").strip()
    try:
        if ch == "music":
            m = SinkMusic()
            line, label, _ = _music_now_status(m, 20, hide_idle=True, bar=False)
            label = " ".join((label or "").split())
            clock = (line or "").lstrip("▶⏸○ ").strip()
            if not label:
                return ""
            return (f"I'm listening to music: {label}"
                    + (f" [{clock}]" if clock else ""))
        if ch == "book":
            srv = _srv()
            np = srv.book_now_playing(target="")
            if np.get("idle"):
                return ""
            title = (np.get("title") or np.get("media_title")
                     or np.get("uri") or "").strip()
            chap = (np.get("chapter_title") or "").strip()
            pos = float(np.get("position_ms") or 0) / 1000.0
            dur = float(np.get("duration_ms") or 0) / 1000.0
            clock = f"{_hms(pos)} / {_hms(dur)}" if dur else _hms(pos)
            what = " — ".join(p for p in (title, chap) if p)
            if not what:
                return ""
            return f"I'm listening to an audiobook: {what} [{clock}]"
        # speech (default): the clip I'm hearing right now.
        np = _now_speaking()
        if not np:
            return ""
        ex = np.get("extras") or {}
        ctx = ((ex.get("current_sentence") or "").strip()
               or (ex.get("text") or "").strip())
        ctx = " ".join(ctx.split())
        if not ctx:
            return ""
        return f'From the agent speech I\'m listening to: "{ctx}"'
    except Exception:  # noqa: BLE001 — the popup must never see a traceback
        return ""


def cmd_ask_context(a) -> int:
    """Print the listening-context blurb for CHANNEL (see `_ask_context`)."""
    print(_ask_context(getattr(a, "channel", "") or "speech"))
    return 0


def cmd_open_pi(a) -> int:
    """Open a fresh pi window seeded with my listening context + a question.

    The music/book/speech analogue of speech's `g`: the popup's `a` key reads a
    question, and this opens a new tmux window running the user's pi launcher
    with `"<listening context>\n\n<question>"` as the first message — a new
    conversation *about what I'm hearing*, which is what a listening-context ask
    almost always is (the player is the context's origin, not an ongoing chat).

    The launcher is `MEDIA_PI_CMD` (default `p` — the user's pi-in-nvim wrapper,
    a zsh function, so it's run through `zsh -ic`; set e.g. `p -c` to continue
    the last session, or `pi` for raw pi instead of the nvim wrapper).
    """
    question = (getattr(a, "question", "") or "").strip()
    context = _ask_context(getattr(a, "channel", "") or "speech")
    prompt = "\n\n".join(p for p in (context, question) if p)
    if not prompt:
        return 1
    pi_cmd = os.environ.get("MEDIA_PI_CMD", "p")
    # `p` is a zsh *function* (pi-in-nvim), so it only resolves in an
    # interactive zsh; `zsh -ic` gets us that. shlex-quote twice: once for the
    # inner `p '<prompt>'`, once to hand that whole line to zsh as one arg.
    inner = f"{pi_cmd} {shlex.quote(prompt)}"
    cmd = f"zsh -ic {shlex.quote(inner)}"
    try:
        subprocess.run(["tmux", "new-window", cmd], capture_output=True)
    except Exception:  # noqa: BLE001
        return 1
    return 0


def _print_open_url(url: str) -> int:
    """Print a URL for client-side opening.

    The tmux popup consumes stdout and presents it to the attached client as an
    OSC 8 link. Avoid opening a browser on the media host in that path; it is
    usually a headless SSH/tmux server, not the device in the user's hand.
    """
    print(url)
    if (os.environ.get("MEDIA_WEB_PRINT_ONLY") or "").strip() == "1":
        return 0
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") \
            or os.environ.get("BROWSER"):
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    return 0


def _first_url(raw: str, fallback: str = "") -> str:
    return (raw.replace(",", " ").split() or [fallback])[0]


def _tailscale_magicdns_name() -> str:
    """This node's short MagicDNS name for URLs opened from another tailnet device."""
    try:
        r = subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                           text=True, timeout=3)
        data = json.loads(r.stdout or "{}") if r.returncode == 0 else {}
        self_node = data.get("Self") or {}
        # MagicDNS adds the tailnet search domain, so the short name keeps the
        # popup link readable while still opening on phones/laptops in the tailnet.
        name = str(self_node.get("HostName") or "").strip()
        if name:
            return name
        name = str(self_node.get("DNSName") or "").strip().rstrip(".")
        if name:
            return name
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        pass
    return ""


def _canvas_web_url() -> str:
    if raw := os.environ.get("MEDIA_CANVAS_URL"):
        return _first_url(raw)
    try:
        port = int(os.environ.get("MEDIA_VISUAL_PORT") or "8781")
    except ValueError:
        port = 8781
    if host := (os.environ.get("MEDIA_VISUAL_MAGICDNS_HOST")
                or _tailscale_magicdns_name()):
        return f"http://{host}:{port}/"
    if raw := os.environ.get("MEDIA_VISUAL_URL"):
        return _first_url(raw)
    return f"http://127.0.0.1:{port}"


def cmd_speech_web(a) -> int:
    """Open the visual canvas for the speech channel."""
    return _print_open_url(_canvas_web_url())


def cmd_book_web(a) -> int:
    """Open the simple-mpv-webui browser control page for the book channel.

    Set MEDIA_BOOK_WEB_URL to the host running the webui. Prefer a bare tailnet
    IP over a MagicDNS name: it's short enough to show on one line in the popup
    (Termux's long-press URL detection has no OSC 8) and resolves without
    MagicDNS. Defaults to loopback.
    """
    return _print_open_url(os.environ.get(
        "MEDIA_BOOK_WEB_URL", "http://127.0.0.1:8889/"))


def cmd_music_web(a) -> int:
    """Open the Mopidy-Iris web UI for the music channel (the music analogue of
    book-web). Set MEDIA_MUSIC_WEB_URL for the same reasons. Defaults to loopback.
    """
    return _print_open_url(os.environ.get(
        "MEDIA_MUSIC_WEB_URL", "http://127.0.0.1:6680/iris/"))


def cmd_highlight_toggle(a) -> int:
    """Toggle auto-highlight on/off. Prints the new state.

    Turning it on jumps focus to the speaking pane (so the copy-mode
    follow-along is actually visible) and highlights the current sentence
    immediately for feedback.
    """
    from .intake.submit import toggle_auto_highlight, _tmux_highlight_text
    on = toggle_auto_highlight()
    # Prefer the pane that produced the speech; fall back to the popup's
    # caller pane if we never captured a source pane.
    pane = (_spoken_pane()
            or os.environ.get("TTS_POPUP_PANE")
            or os.environ.get("TMUX_PANE", ""))
    if on:
        if pane:
            # Jump to the speaking pane so the follow-along is on screen.
            _focus_pane(pane)
            os.environ["TMUX_PANE"] = pane
            if not os.environ.get("TMUX"):
                os.environ["TMUX"] = "x"
            # If a sentence is playing right now, highlight it immediately.
            np = _now_speaking()
            sentence = (np.get("extras") or {}).get("current_sentence") if np else None
            if sentence:
                _tmux_highlight_text(sentence, force=True)
        print("highlight: ON")
    else:
        # Kill any in-flight clear-timer (so it can't fire into the pane after
        # we're off), then force copy-mode shut and verify it actually left —
        # otherwise the pane stays in tmux copy-mode, eating the app's own
        # scroll/transcript keys even though highlighting is "off".
        if pane:
            from .intake.submit import (_kill_pending_clear,
                                        _force_cancel_copy_mode)
            _kill_pending_clear(pane)
            _force_cancel_copy_mode(pane)
        print("highlight: OFF")
    return 0


def cmd_highlight_now(a) -> int:
    """Force highlight follow-along for the upcoming turn(s), bypassing the
    keystroke-skip, until the user types again. Bound to tmux `prefix V`.

    The keystroke-skip suppresses the highlight for a turn when you've just
    typed (so it doesn't yank copy-mode out from under you). This says "I've
    stopped — follow along now" without waiting it out. If a sentence is
    already playing, it highlights it immediately for instant feedback.
    """
    from .intake.submit import (set_force_highlight, _is_auto_highlight_enabled,
                                _tmux_highlight_text)
    set_force_highlight()
    if not _is_auto_highlight_enabled():
        # Force only overrides the keystroke-skip, not the master opt-in.
        print("highlight: armed (note: auto-highlight is OFF — toggle it on)")
        return 0
    pane = (_spoken_pane()
            or os.environ.get("TTS_POPUP_PANE")
            or os.environ.get("TMUX_PANE", ""))
    if pane:
        os.environ["TMUX_PANE"] = pane
        if not os.environ.get("TMUX"):
            os.environ["TMUX"] = "x"
        np = _now_speaking()
        sentence = (np.get("extras") or {}).get("current_sentence") if np else None
        if sentence:
            _tmux_highlight_text(sentence, force=True)
    print("highlight: now (until you type again)")
    return 0


def cmd_current_sentence(a) -> int:
    """Print the currently-spoken sentence (one of many in a response).

    Designed for tmux status-line use: shows a karaoke-style indicator of
    what's being read aloud right now, without touching the source pane.
    Truncates to --width chars (default 80) with an ellipsis so it fits.
    """
    np = _now_speaking()
    if not np:
        return 0
    ex = np.get("extras") or {}
    sentence = (ex.get("current_sentence") or "").strip()
    if not sentence:
        return 0
    sentence = " ".join(sentence.split())  # collapse whitespace
    width = getattr(a, "width", 80) or 80
    if len(sentence) > width:
        sentence = sentence[: max(0, width - 1)].rstrip() + "…"
    print(f"♪ {sentence}")
    return 0


def cmd_text(a) -> int:
    """Return the currently-speaking text, or the latest history entry if idle."""
    np = _now_speaking()
    if np:
        txt = (np["extras"].get("text") or "").strip()
        if txt:
            print(txt)
            return 0
    rows = _speech_history(1)
    if rows:
        txt = (rows[0].get("text") or "").strip()
        if txt:
            print(txt)
    return 0


def _history_index_for_pane(pane: str, limit: int = 50) -> Optional[int]:
    """1-based index into recent speech history of the latest clip produced
    by `pane` (1 = most recent overall). None if the pane has no clip."""
    if not pane:
        return None
    for i, r in enumerate(_speech_history(limit), start=1):
        ex = r.get("extras") or {}
        if isinstance(ex, str):
            try:
                ex = json.loads(ex)
            except json.JSONDecodeError:
                ex = {}
        if ex.get("source_pane") == pane:
            return i
    return None


def _patch_speech_mirror(**live) -> None:
    """Optimistically patch the speech now_playing mirror (live_pause/speed/mute)
    so a control shows up in the popup on its very next redraw, instead of
    lagging ~1s until the intake monitor re-reads the remote player. The monitor
    overwrites these with ground truth on its next tick, so a stale patch is
    self-correcting."""
    store = StateStore()
    np = store.get_now_playing("speech")
    if not np:
        return
    ex = np.get("extras")
    if not isinstance(ex, dict):
        return
    ex.update(live)
    store.set_now_playing("speech", uri=np["uri"], started_at=np["started_at"],
                          target=np.get("target") or "local",
                          content_type=np.get("content_type"), extras=ex)


def cmd_toggle(a) -> int:
    # If nothing is loaded, "play" means replay a clip (matches the old
    # popup's Space = play/pause-or-replay). Prefer the most recent clip from
    # the *active* pane (the one that opened the popup), so Space-while-idle
    # replays "what this pane just said"; fall back to the latest overall.
    # Otherwise flip pause.
    if _remote_speech():
        # Over the phone bridge each get_property is a full ~600ms round-trip,
        # so the old idle-read + pause-read + pause-write cost ~2s (and could hit
        # a "property unavailable" retry storm). Decide from the local mirror and
        # do ONE idempotent write; patch the mirror so the glyph flips at once.
        idle, _pos, _dur, paused, _muted, _speed, playing = _speech_display_state()
        if not playing:
            pane = os.environ.get("TTS_POPUP_PANE") or os.environ.get("TMUX_PANE", "")
            return _do_replay(_history_index_for_pane(pane) or 1)
        new_pause = not paused
        # Fire-and-forget: pausing suspends the phone's audio device (~0.6s), but
        # we don't need to wait for that "ok" — the mirror patch is what the popup
        # reads back and the monitor confirms ground truth next tick. Returns in
        # ~0.3s (connect+send) instead of ~0.7s. Falls back to a waited set.
        try:
            ipc.send_nowait(_sock(), "set_property", "pause", new_pause)
        except Exception:  # noqa: BLE001
            ipc.set_property(_sock(), "pause", new_pause)
        _patch_speech_mirror(live_pause=new_pause)
        return 0
    if _get("idle-active"):
        pane = os.environ.get("TTS_POPUP_PANE") or os.environ.get("TMUX_PANE", "")
        return _do_replay(_history_index_for_pane(pane) or 1)
    ipc.set_property(_sock(), "pause", not bool(_get("pause")))
    return 0


def cmd_pause(a) -> int:
    SinkSpeech().pause(_active_speech_target())
    return 0


def cmd_resume(a) -> int:
    SinkSpeech().resume(_active_speech_target())
    return 0


def cmd_stop(a) -> int:
    SinkSpeech().stop(_active_speech_target())
    return 0


def cmd_seek(a) -> int:
    ipc.command(_sock(), "seek", a.secs, "relative")
    return 0


def cmd_volume(a) -> int:
    cur = _get("volume") or 100
    ipc.set_property(_sock(), "volume", max(0, min(150, int(cur) + a.delta)))
    return 0


def cmd_mute(a) -> int:
    ipc.set_property(_sock(), "mute", not bool(_get("mute")))
    return 0


# --- durable per-pane / per-session mute (Step 3/4) -------------------------

def _live_panes() -> list[str]:
    """Current tmux pane ids across all sessions, or [] if tmux is unreachable.

    [] means "couldn't determine" — callers must treat it as such and never
    use it to prune (see StateStore.prune_panes, which no-ops on an empty set).
    """
    try:
        r = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
                           capture_output=True, text=True)
    except Exception:  # noqa: BLE001
        return []
    return r.stdout.split() if r.returncode == 0 else []


def _mute_target_pane(a) -> str:
    """Resolve which pane a mute command acts on.

    Precedence: explicit --pane → --subject (the popup's subject: what's
    playing now, else the caller pane) → --current (legacy: the speaking/last
    pane) → the calling shell's $TMUX_PANE → the speaking pane as a last resort.
    The popup uses --subject so `M` acts on the same thing the title shows.
    """
    if getattr(a, "pane", None):
        return a.pane
    if getattr(a, "subject", False):
        return _subject()[0]
    if getattr(a, "current", False):
        return _spoken_pane() or ""
    return os.environ.get("TMUX_PANE", "") or (_spoken_pane() or "")


def _silence_current_if_covered(scope: str, key: str) -> bool:
    """Stop the speech broker if it's *actively* playing a clip from a pane the
    mute now covers, so `M` feels immediate (like `m`) instead of only
    suppressing the next response. The in-flight response is already in history,
    so it stays replayable. Returns True if it stopped something.
    """
    np = _now_speaking()                 # active playback only (not history)
    if not np:
        return False
    ex = np.get("extras") or {}
    covered = (ex.get("source_pane") == key if scope == "pane"
               else ex.get("source_tmux_session") == key)
    if covered:
        try:
            SinkSpeech().stop(_active_speech_target())
        except Exception:  # noqa: BLE001 — a dead/absent broker mustn't fail the mute
            pass
        return True
    return False


def cmd_mute_pane(a) -> int:
    """Set/clear durable per-pane (or --session) speech mute. Default toggles.

    A muted pane still renders + records history (the popup can replay it) but
    is never played live and never ducks music — enforced at intake. Muting
    also stops the covered pane's currently-playing clip, if any.
    """
    state = StateStore()
    pane = _mute_target_pane(a)
    if not pane:
        print("media mute-pane: no target pane (not in tmux and nothing "
              "speaking) — pass --pane %ID", file=sys.stderr)
        return 1
    from .intake.submit import _tmux_session_for_pane
    session = _tmux_session_for_pane(pane)
    action = getattr(a, "state", None) or "toggle"

    if getattr(a, "session", False):
        if not session:
            print(f"media mute-pane: could not resolve a tmux session for "
                  f"{pane}", file=sys.stderr)
            return 1
        scope, key = "session", session
        if action == "on":
            new = True
        elif action == "off":
            new = False
        else:  # toggle this session's own override
            new = not bool(state.get_mute("session", key))
    else:
        scope, key = "pane", pane
        if action == "on":
            new = True
        elif action == "off":
            new = False
        else:  # toggle the *effective* state, so it flips what you actually hear
            new = not state.resolve_mute(pane, session)

    state.set_mute(scope, key, new)
    stopped = _silence_current_if_covered(scope, key) if new else False
    print(f"{scope} {key}: {'muted' if new else 'unmuted'}"
          f"{' (stopped current)' if stopped else ''}")
    return 0


def cmd_mute_status(a) -> int:
    """List per-pane / per-session mutes, pruning since-closed panes first."""
    state = StateStore()
    live = _live_panes()
    if live:
        state.prune_panes(live)   # only when tmux gave a reliable snapshot
    live_set = set(live)
    mutes = state.list_mutes()
    panes, sessions = mutes["panes"], mutes["sessions"]
    if not panes and not sessions:
        print("no per-pane or per-session mutes set")
        return 0
    for key, m in sorted(panes.items()):
        tag = "" if key in live_set else " (dead)"
        print(f"pane    {key}{tag}: {'muted' if m else 'unmuted'}")
    for key, m in sorted(sessions.items()):
        print(f"session {key}: {'muted' if m else 'unmuted'}")
    return 0


def cmd_mute_count(a) -> int:
    """Print the total number of muted panes + sessions (nothing when zero).

    Drives the popup's "you have N things muted" badge so a durable mute set
    on a pane you're not looking at doesn't silently stay forgotten.
    """
    m = StateStore().list_mutes()
    n = sum(1 for v in m["panes"].values() if v) + \
        sum(1 for v in m["sessions"].values() if v)
    if n:
        print(n)
    return 0


def cmd_pane_muted(a) -> int:
    """Print '1' when the popup's *subject* pane is effectively muted.

    Drives the popup's 🔒 indicator. Resolves the same subject as the title
    and `M` (`_subject()`: what's playing now, else the caller pane), so the
    glyph always reflects exactly what `M` would toggle. An explicit `--pane`
    overrides. Silent (prints nothing) when unmuted or unresolvable.
    """
    pane = getattr(a, "pane", None)
    sess = ""
    if not pane:
        pane, sess, _ = _subject()
    if not pane:
        return 0
    if not sess:
        sess = _tmux_session_for_pane(pane)
    if StateStore().resolve_mute(pane, sess):
        print("1")
    return 0


# Speed [ / ] ladder. At/above 1.0x, presses hop these rungs — the gaps widen
# (1.0→1.25→1.5→2.0→3.0) so a held key accelerates. Below 1.0x, fine
# flat 0.1 steps for precise control (no ladder). Symmetric for up/down. As a
# position ladder (snap off the live speed) it needs no cross-press accel state —
# each listening-mode [ / ] press is a separate `media speed` process.
# Keep in sync with SPEED_RUNGS in tmux/media-popup (book channel's shell copy).
_SPEED_MIN, _SPEED_MAX, _SPEED_FLAT = 0.3, 3.0, 0.1
_SPEED_RUNGS = (1.0, 1.25, 1.5, 2.0, 3.0)


def _speed_next(cur: float, direction: int) -> float:
    """Next speed for a [ / ] press: +1 faster / -1 slower. Hops _SPEED_RUNGS
    at/above 1.0x; flat _SPEED_FLAT steps below. Clamped to [_SPEED_MIN, _SPEED_MAX]."""
    eps = 1e-6
    if direction > 0:
        if cur < 1.0 - eps:
            return min(round(cur + _SPEED_FLAT, 2), 1.0)
        for r in _SPEED_RUNGS:
            if r > cur + eps:
                return r
        return _SPEED_MAX
    if cur > 1.0 + eps:
        for r in reversed(_SPEED_RUNGS):
            if r < cur - eps:
                return r
        return 1.0
    return max(round(cur - _SPEED_FLAT, 2), _SPEED_MIN)


def cmd_speed(a) -> int:
    """Set speech speed: absolute factor, 'reset' (→1.0), or relative 'up'/'down'
    (the listening-mode [ / ] keys) which snap the live sink along the speed ladder.
    The raw '+0.1' / '-0.1' forms still apply a literal delta. Clamped to range."""
    sock = _sock()
    f = a.factor

    def _cur() -> float:
        # For a remote target read the live speed off the local mirror rather
        # than paying a bridge round-trip (matches cmd_toggle).
        if _remote_speech():
            sp = _speech_display_state()[5]
            return float(sp) if isinstance(sp, (int, float)) else 1.0
        cur = _get("speed")
        return float(cur) if isinstance(cur, (int, float)) else 1.0

    if f == "reset":
        target = 1.0
    elif f in ("up", "down"):
        target = _speed_next(_cur(), 1 if f == "up" else -1)
    elif f and f[0] in "+-":
        target = max(_SPEED_MIN, min(_SPEED_MAX, _cur() + float(f)))
    else:
        target = max(_SPEED_MIN, min(_SPEED_MAX, float(f)))
    target = round(target, 2)
    ipc.set_property(sock, "speed", target)
    if _remote_speech():
        _patch_speech_mirror(live_speed=target)
    return 0


def _seek_to_end(sock) -> int:
    """Skip to the end of the (last) clip so the response finishes."""
    # A seek-to-end only plays out if the broker isn't paused/muted: a paused
    # clip just parks the playhead at 100% and never reaches EOF (so the popup
    # `>` looked like a no-op when the clip had been paused, e.g. via Space).
    # Clear those first so the clip actually finishes.
    for prop in ("pause", "mute"):
        try:
            ipc.set_property(sock, prop, False)
        except ipc.MpvIpcError:
            pass
    # On a multi-clip replay the response's clips are queued as one mpv
    # playlist; seeking the *current* clip to 100% would only advance to the
    # next one. Jump to the final playlist entry first so we land on the
    # actual last clip before seeking it to the end — but only if we're not
    # already on it. Re-setting playlist-pos to the index that's already
    # current makes mpv *reload* that entry (restart it from 0): that was the
    # "`>` repeated the current clip instead of ending it" bug, hit whenever
    # the popup's `>` landed while the last clip was already playing.
    try:
        count = ipc.get_property(sock, "playlist-count")
        pos = ipc.get_property(sock, "playlist-pos")
        if isinstance(count, int) and count > 1 and pos != count - 1:
            ipc.set_property(sock, "playlist-pos", count - 1)
    except ipc.MpvIpcError:
        pass
    ipc.command(sock, "seek", 100, "absolute-percent")
    return 0


def cmd_jump(a) -> int:
    """Seek to the start or end of the current clip."""
    sock = _sock()
    if a.where == "start":
        ipc.command(sock, "seek", 0, "absolute")
        return 0
    # End-of-response. On a *replay* the clips are queued as one mpv playlist,
    # so seeking the last entry to its end finishes the whole response. During
    # a *live* readout each sentence is a separate loadfile (playlist-count 1):
    # seeking the current clip to EOF would just let the reader loop advance to
    # the next sentence — making `>` behave like `l`. Hand the reader a
    # past-the-end jump so it stops after the current clip instead of
    # continuing, then seek the current clip out so playback ends promptly.
    np = StateStore().get_now_playing("speech")
    ex = (np or {}).get("extras") or {}
    if isinstance(ex, str):
        try:
            ex = json.loads(ex)
        except json.JSONDecodeError:
            ex = {}
    sentences = ex.get("clip_sentences") or []
    try:
        count = ipc.get_property(sock, "playlist-count")
    except ipc.MpvIpcError:
        count = 1
    playlist = isinstance(count, int) and count > 1
    if len(sentences) > 1 and not playlist:
        _write_nav_request(len(sentences),
                           (np or {}).get("target") or SPEECH_TARGET.name)
    return _seek_to_end(sock)


def _nav_target(cur: int, n: int, para_idx: list, unit: str,
                direction: int) -> int:
    """Resolve the sentence index to jump to for `media skip`.

    A return >= n means "past the last section" → finish the response; a
    negative return is clamped to 0 by the caller (restart the first section).
    """
    if unit == "sentence":
        return cur + (1 if direction > 0 else -1)
    # paragraph
    if not para_idx or cur >= len(para_idx):
        return cur + (1 if direction > 0 else -1)
    cur_para = para_idx[cur]
    if direction > 0:
        nxt = [p for p in para_idx if p > cur_para]
        if not nxt:
            return n  # already in the last paragraph → finish
        tp = min(nxt)
        return next(j for j in range(n) if para_idx[j] == tp)
    # backward: to the start of the current paragraph, else the previous one's
    para_start = next(j for j in range(n) if para_idx[j] == cur_para)
    if cur > para_start:
        return para_start
    prev = [p for p in para_idx if p < cur_para]
    if not prev:
        return 0
    tp = max(prev)
    return next(j for j in range(n) if para_idx[j] == tp)


def _force_highlight_sentence(sentence: str) -> None:
    """Force the copy-mode highlight onto `sentence` (used for replay jumps)."""
    from .intake.submit import _tmux_highlight_text
    pane = os.environ.get("TTS_POPUP_PANE") or os.environ.get("TMUX_PANE", "")
    if "#{" in pane:
        pane = ""
    if not pane:
        return
    os.environ["TMUX_PANE"] = pane
    if not os.environ.get("TMUX"):
        os.environ["TMUX"] = "x"
    try:
        _tmux_highlight_text(sentence, force=True)
    except Exception:  # noqa: BLE001 — popup must never see a traceback
        pass


def _write_nav_request(idx: int, target_name: str = "local") -> None:
    """Drop a jump request the live reader loop reads after the current clip.

    Keyed by the *playing* target (e.g. "rooms" for the Snapcast feed) so the
    flag filename matches what the reader loop polls — the loop runs with
    MEDIA_SPEECH_DEFAULT_TARGET, which isn't necessarily "local".
    """
    from .intake.submit import _nav_flag_path
    try:
        _nav_flag_path(Target(name=target_name)).write_text(str(idx))
    except OSError:
        pass


# Rapid skip presses must chain: each press is one step from where the LAST
# press pointed, not from a re-read of "current sentence" — the playlist-pos /
# mirror reads can lag a quick second press (bridge latency, mirror tick), so
# re-deriving would compute the same target and merely replay the sentence the
# first press chose. The breadcrumb holds the last commanded index, honored
# while presses cluster within this window.
_SKIP_CHAIN_S = 3.0


def _skip_cursor_path() -> Path:
    return state_dir() / f"skip-cursor-{SPEECH_TARGET.name}"


def _read_skip_cursor() -> Optional[int]:
    try:
        p = _skip_cursor_path()
        if time.time() - p.stat().st_mtime > _SKIP_CHAIN_S:
            return None
        return int(p.read_text().strip())
    except (OSError, ValueError):
        return None


def _write_skip_cursor(idx: int) -> None:
    try:
        p = _skip_cursor_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(idx))
    except OSError:
        pass


def cmd_skip(a) -> int:
    """Step the speech reader forward/back by a sentence (h/l) or paragraph (H/L).

    Works both on a replay (clips queued as one mpv playlist → jump by
    playlist-pos) and during the live readout (the reader loop picks up a jump
    request even while paused). Falls back to a plain time-seek of
    --seek-fallback seconds when there's no multi-sentence sequence to step.
    """
    sock = _sock()
    direction = 1 if a.dir > 0 else -1

    def _time_seek() -> int:
        try:
            ipc.command(sock, "seek", float(a.seek_fallback), "relative")
            return 0
        except ipc.MpvIpcError:
            return 1

    np = StateStore().get_now_playing("speech")
    ex = (np or {}).get("extras") or {}
    if isinstance(ex, str):
        try:
            ex = json.loads(ex)
        except json.JSONDecodeError:
            ex = {}
    sentences = ex.get("clip_sentences") or []
    para_idx = ex.get("clip_paragraph_idx") or []
    n = len(sentences)

    try:
        idle = bool(ipc.get_property(sock, "idle-active"))
    except ipc.MpvIpcError:
        idle = True
    try:
        raw = ipc.get_property(sock, "playlist-count")
        count = int(raw) if isinstance(raw, int) else 1
    except ipc.MpvIpcError:
        count = 1

    if n <= 1 or idle:
        return _time_seek()
    if len(para_idx) != n:
        para_idx = list(range(n))  # no paragraph map → one paragraph per line

    playlist = count > 1
    if playlist:
        try:
            cur = int(ipc.get_property(sock, "playlist-pos") or 0)
        except ipc.MpvIpcError:
            cur = 0
    else:
        cur = ex.get("current_sentence_idx")
        if cur is None:
            return _time_seek()
        cur = int(cur)

    # A press within the chain window steps from the LAST press's target,
    # whatever the (possibly lagging) live read said.
    crumb = _read_skip_cursor()
    if crumb is not None and 0 <= crumb < n:
        cur = crumb

    target = _nav_target(cur, n, para_idx, a.unit, direction)
    if target < 0:
        target = 0
    _write_skip_cursor(min(target, n - 1))

    if playlist:
        if target >= n:
            return _seek_to_end(sock)
        try:
            ipc.set_property(sock, "playlist-pos", target)
        except ipc.MpvIpcError:
            return 1
        # Rapid presses race mpv's async entry loads: an earlier in-flight
        # jump can commit AFTER ours and clobber it (observed as a skip
        # "bouncing back" a moment later). Verify once, best-effort.
        try:
            time.sleep(0.15)
            if int(ipc.get_property(sock, "playlist-pos") or -1) != target:
                ipc.set_property(sock, "playlist-pos", target)
        except (ipc.MpvIpcError, TypeError, ValueError):
            pass
        _force_highlight_sentence(sentences[target])
        return 0
    # Live readout: hand the jump to the reader loop (honored even while
    # paused). Key the flag by the target that's actually playing, falling
    # back to the CLI's resolved speech target — NOT "local", which orphans
    # the flag whenever now_playing lacks a target (the reader polls the
    # actual playout target's flag).
    _write_nav_request(target, (np or {}).get("target") or SPEECH_TARGET.name)
    return 0


def _replay_visual(extras: dict) -> None:
    """Re-show the visual that accompanied a replayed reply. A replay means
    "that again" — for a figure-bearing reply the picture IS part of it, and
    without this it plays under whatever newer artwork holds the canvas.
    Best-effort: needs the visual package's push memory and live spool files."""
    key = (extras or {}).get("dedup_key")
    if not key:
        return
    try:
        from agent_media_visual.state import load_push, spool_dir
    except ImportError:
        return
    payload = load_push(str(key))
    if not payload:
        return
    # Spool-relative names must still exist (GC keeps ~200); absolute /img/
    # URLs can't be checked from here — push and let the canvas 404 quietly.
    names = ([payload.get("image")] if payload.get("image")
             else [b.get("image") for b in payload.get("sequence") or []])
    for nm in names:
        if nm and "/" not in str(nm) and not (spool_dir() / str(nm)).is_file():
            return
    import urllib.request
    urls = (os.environ.get("MEDIA_VISUAL_URL") or "").replace(",", " ").split()
    for base in urls:
        try:
            req = urllib.request.Request(
                base.rstrip("/") + "/show",
                data=json.dumps(payload).encode(),
                method="POST", headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=2).read()
        except OSError:
            pass


def _do_replay(index: int, session: Optional[str] = None) -> int:
    rows = _speech_history(max(1, index), session=session)
    if len(rows) < index:
        print("media: no clip to replay", file=sys.stderr)
        return 1
    row = rows[index - 1]
    uri = row.get("uri")
    if not uri:
        return 1
    ex = row.get("extras") or {}
    clip_uris: list[str] = ex.get("clip_uris") or [uri]
    clip_durations: list[float] = ex.get("clip_durations_s") or []
    replay_text: str = row.get("text") or ""

    # Re-show the reply's visual concurrently with the (slow, bridge-bound)
    # playback push below; the thread outlives neither — the process waits.
    threading.Thread(target=_replay_visual, args=(ex,)).start()

    sink = SinkSpeech()
    # A remote target plays clips from its clips-relay dir, which only the
    # live intake path populates — and a replayed item's clips may never have
    # arrived (e.g. rendered while ssh to the phone was down) or may have been
    # cleaned since. Re-push them first; on failure the sink resolves clips to
    # the HTTP base URL instead. No-op for local/rooms, and cheap (one
    # multiplexed ssh hop) when the files are already there.
    getattr(sink, "prefetch", lambda *a, **k: True)(clip_uris, SPEECH_TARGET)
    # Push the whole turn in ONE batched round-trip (stop/clear/append-all/
    # unpause/jump-to-0) rather than 1 play + N queues + 2 state-sets — each a
    # ~600ms hop over the phone bridge. Traversing (< / >) or replaying a long
    # multi-clip turn otherwise drove every clip individually, blocking the
    # popup for seconds per press (a 14-clip reply ≈ 8s frozen); mashing back
    # through a few clips then looked like the popup had hung. Mirrors the live
    # intake path (play_playlist), which also clears any lingering pause/mute so
    # a "replay" ("I want to hear this now") is audible past a stale pause/mute.
    if len(clip_uris) > 1:
        sink.play_playlist(clip_uris, SPEECH_TARGET)
    else:
        # Single clip: one loadfile + explicit state reset. OSError too — a
        # missing/refused socket (mpv not up yet) must be a no-op, not a
        # traceback (_open raises raw FileNotFoundError/ConnectionRefused).
        sink.play(clip_uris[0], SPEECH_TARGET)
        try:
            ipc.set_property(_sock(), "pause", False)
            ipc.set_property(_sock(), "mute", False)
        except (ipc.MpvIpcError, OSError):
            pass
    clip_sentences: list[str] = ex.get("clip_sentences") or []
    have_durations = (
        len(clip_durations) == len(clip_uris) and len(clip_durations) > 0
    )
    # Always refresh now_playing so cmd_status's progress bar reflects the
    # clip we just started, not a stale prior entry. Without this, replaying
    # a single-clip history item (the common `<` case) left the previous
    # response's total_duration_s in place and the bar never acknowledged
    # the jump. When we have per-clip durations, persist them so cmd_status
    # can compute a spanning bar; otherwise omit total_duration_s and let
    # cmd_status fall back to mpv's raw time-pos/duration.
    np_extras: dict = {"text": replay_text}
    source_pane = ex.get("source_pane")
    if source_pane:
        np_extras["source_pane"] = source_pane
    # Carry the clip's conversation (Claude id) + tmux session forward so the
    # next < / > press anchors to the same conversation — keeps the traversal
    # scope stable across the walk (_anchor_session reads source_session).
    src_claude = ex.get("source_session")
    if src_claude:
        np_extras["source_session"] = src_claude
    src_sess = ex.get("source_tmux_session")
    if src_sess:
        np_extras["source_tmux_session"] = src_sess
    src_window = ex.get("source_window")
    if src_window:
        np_extras["source_window"] = src_window
    if have_durations:
        np_extras["total_duration_s"] = sum(clip_durations)
        np_extras["clip_durations_s"] = clip_durations
    # Carry the sentence + paragraph map so `media skip` can step the replay
    # by sentence/paragraph; the tracker keeps current_sentence_idx fresh.
    if clip_sentences and len(clip_sentences) == len(clip_uris):
        np_extras["clip_sentences"] = clip_sentences
        cpi = ex.get("clip_paragraph_idx")
        if cpi and len(cpi) == len(clip_uris):
            np_extras["clip_paragraph_idx"] = cpi
        np_extras["current_sentence_idx"] = 0
    if have_durations:
        # Spawn a detached follower so the replay behaves like live playback
        # even though _do_replay returns immediately: it mirrors the player's
        # live position into now_playing (else the popup's bar sits frozen at
        # 00:00 for the whole replay — and forever after, since nothing would
        # clear the row) and, for multi-clip turns with a pane, fires the
        # copy-mode highlight per sentence.
        # TTS_POPUP_PANE is the original pane that opened the popup; TMUX_PANE
        # inside display-popup is the popup's own ephemeral pane.
        pane = _caller_pane()
        # Supersede any tracker still polling from a prior replay. The
        # tracker only self-exits when the speech mpv goes idle, so
        # replaying again before the prior playlist finishes (rapid < / >
        # traversal, re-pressing r/Space) would otherwise leave the old
        # tracker running on the shared socket — it never sees "its"
        # playback end and keeps highlighting the new clip with the old
        # clip's sentences. killpg the previous one (start_new_session ⇒
        # the child's pid is its own pgid). Mirrors the per-pane pidfile
        # pattern _tmux_highlight_text uses for its clear-timer. Killed
        # BEFORE the set_now_playing below so a dying tracker can never
        # race a clear against the fresh row.
        import re as _re
        import signal as _signal
        _pane_safe = _re.sub(r"[^A-Za-z0-9_-]", "_", pane) if pane else "nopane"
        _trk_pidfile = f"/tmp/media-replay-track-{_pane_safe}.pid"
        try:
            with open(_trk_pidfile) as _f:
                _old_pgid = int(_f.read().strip())
            os.killpg(_old_pgid, _signal.SIGTERM)
        except (OSError, ValueError, ProcessLookupError, PermissionError):
            pass
        # Highlight only multi-clip turns from a known pane (single-clip
        # replays never highlighted); the position mirror runs regardless.
        _hl = (pane and len(clip_uris) > 1
               and clip_sentences and len(clip_sentences) == len(clip_uris))
        _trk = subprocess.Popen(
            [sys.executable, "-m", "agent_media_core.cli",
             "replay-track",
             "--sentences", json.dumps(clip_sentences) if _hl else "",
             "--pane", pane,
             "--durations", json.dumps(clip_durations)],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            with open(_trk_pidfile, "w") as _f:
                _f.write(str(_trk.pid))
        except OSError:
            pass
        # Stamp the follower as the row's writer: the store's orphan guard
        # then self-heals the row if the tracker dies uncleanly, instead of
        # the bar freezing at its last mirrored position forever.
        np_extras["writer_pid"] = _trk.pid
    StateStore().set_now_playing(
        "speech", uri=clip_uris[0], started_at=time.time(),
        target=SPEECH_TARGET.name, extras=np_extras)
    return 0


def cmd_replay(a) -> int:
    # Scope < / > / r traversal to the current tmux session's clips.
    return _do_replay(a.index, session=_anchor_session())


def _prev_restart_threshold() -> float:
    """Seconds into an item past which `<` restarts it instead of stepping back."""
    try:
        return max(0.0, float(os.environ.get("MEDIA_POPUP_PREV_RESTART_S") or 3.0))
    except (TypeError, ValueError):
        return 3.0


def _prev_with_restart(elapsed, restart, step_back) -> int:
    """⏮ shared by the music/book `<` key: restart the current item if we're
    more than the grace window into it, else step back to the previous one.

    `elapsed` returns seconds into the current item (None/0 when idle → step
    back); `restart` seeks it to 0; `step_back` moves to the previous item.
    """
    try:
        pos = float(elapsed() or 0.0)
    except (TypeError, ValueError):
        pos = 0.0
    (restart if pos > _prev_restart_threshold() else step_back)()
    return 0


def cmd_replay_prev(a) -> int:
    """Popup `<` for speech: "previous" with a restart-first grace window.

    Like a music player's ⏮: if we're already more than
    MEDIA_POPUP_PREV_RESTART_S (default 3s) into the current turn, `<` rewinds
    to that turn's start rather than jumping to the older one. Only when we're
    at/near the start (or nothing's playing) does it step back a turn. `--idx`
    is the popup's current history cursor (1 = latest); the resolved cursor is
    printed to stdout so the popup can update its own `hist_idx`.
    """
    idx = max(1, a.idx)
    idle, pos, _dur, *_ = _speech_display_state()
    session = _anchor_session()
    if (not idle) and pos is not None and pos > _prev_restart_threshold():
        # Partway through the current turn → restart it, keep the cursor put.
        _do_replay(idx, session=session)
        new_idx = idx
    else:
        # At the start (or idle) → step back a turn; stay put if there's none.
        new_idx = idx + 1
        if _do_replay(new_idx, session=session) != 0:
            new_idx = idx
    print(new_idx)
    return 0


def _clip_index_in_text(captured: str) -> Optional[int]:
    """1-based speech-history index of the most-recent clip whose search anchor
    appears in `captured` pane text, or None if none is present. Shared by
    `p`'s copy-mode path (capture down to the cursor) and its fullscreen path
    (capture the visible screen)."""
    from .intake.submit import _anchor_for

    if not captured:
        return None
    # Collapse whitespace before matching: the terminal word-wraps a response
    # at its content width, so an anchor longer than that width spans two visual
    # rows and a raw substring test misses it. The highlight path keeps anchors
    # to one row because it uses a row-bound tmux search; here we do a plain
    # substring test, so normalize both sides and wrapping stops mattering.
    norm_cap = " ".join(captured.split())
    for i, row in enumerate(_speech_history(200), start=1):
        anchor = _anchor_for(row.get("text") or "")
        if anchor and " ".join(anchor.split()) in norm_cap:
            return i
    return None


def _announce_replay(idx: int) -> int:
    """Flash a ♪ preview of clip `idx` (popup `p` feedback) then replay it."""
    rows = _speech_history(200)
    if 1 <= idx <= len(rows):
        preview = " ".join((rows[idx - 1].get("text") or "").split())
        if len(preview) > 60:
            preview = preview[:57] + "…"
        subprocess.run(["tmux", "display-message", f"♪ {preview}"],
                       capture_output=True)
    return _do_replay(idx)


def cmd_replay_at_cursor(a) -> int:
    """Replay the spoken clip at/just-above the copy-mode cursor (popup `p`).

    "The clip in the sequence before the cursor": capture the caller pane's
    text down to the cursor row, then play the most recent clip whose search
    anchor appears in it — clips below the cursor never appear in the capture,
    so they're excluded for free, and most-recent-first picks the nearest
    preceding utterance. Reuses `_anchor_for` so a clip that the auto-highlight
    can land on is exactly one this can match. If the pane isn't scrolled into
    copy-mode there's no cursor to point with, so it falls back to the most
    recent clip on the *visible screen* (this is what makes `p` work in Claude's
    fullscreen mode, which has no scrollback or copy-mode cursor), and failing
    that to "replay what this pane just said" (the latest clip from this pane).
    """
    pane = _caller_pane()
    if not pane:
        print("media: no caller pane", file=sys.stderr)
        return 1
    # Deliberately NOT session-scoped: the pane-scrollback capture below is
    # itself the scope — only a clip whose text is visible in *this* pane can
    # match. Searching all sessions lets `p` play whatever's above the cursor
    # regardless of which session last spoke or owns the clip (the whole point
    # of `p`: play from the cursor, not from the last-played clip).

    # Cursor state in the caller pane (queryable while the popup overlays it).
    in_mode, cur_y, scroll = "", "", ""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane,
             "#{pane_in_mode}\t#{copy_cursor_y}\t#{scroll_position}"],
            capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            parts = r.stdout.rstrip("\n").split("\t")
            in_mode = parts[0] if len(parts) > 0 else ""
            cur_y = parts[1] if len(parts) > 1 else ""
            scroll = parts[2] if len(parts) > 2 else ""
    except Exception:  # noqa: BLE001
        pass

    # Not scrolled into copy-mode → no cursor to point with. Try the *visible
    # screen* first: replay the most recent clip currently on screen. This is
    # what makes `p` useful in Claude's fullscreen (alt-screen) mode, which has
    # no scrollback or copy-mode cursor — capture-pane there returns just the
    # visible screen, so a match means "the clip I can see". Fall back to this
    # pane's latest clip when nothing on screen matches (e.g. the spoken text
    # scrolled off), preserving the old "play what this pane just said".
    if in_mode.strip() != "1" or not cur_y.strip().isdigit():
        try:
            cap = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", pane],
                capture_output=True, text=True, timeout=4)
            visible = cap.stdout if cap.returncode == 0 else ""
        except Exception:  # noqa: BLE001
            visible = ""
        idx = _clip_index_in_text(visible)
        if idx is not None:
            return _announce_replay(idx)
        idx = _history_index_for_pane(pane)
        if idx is None:
            print("media: this pane has no spoken clip", file=sys.stderr)
            return 1
        return _do_replay(idx)

    # capture-pane line numbers are relative to the live screen (0 = top of the
    # visible pane, negative into history); copy_cursor_y is relative to the
    # scrolled copy-mode view. Subtract scroll_position to convert.
    scroll_n = int(scroll) if scroll.strip().isdigit() else 0
    end_line = int(cur_y) - scroll_n
    try:
        cap = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane,
             "-S", "-32768", "-E", str(end_line)],
            capture_output=True, text=True, timeout=4)
        captured = cap.stdout if cap.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        captured = ""
    if not captured:
        print("media: could not read pane text", file=sys.stderr)
        return 1

    idx = _clip_index_in_text(captured)
    if idx is not None:
        return _announce_replay(idx)

    subprocess.run(
        ["tmux", "display-message", "⊘ no spoken clip above cursor"],
        capture_output=True)
    print("media: no spoken clip above cursor", file=sys.stderr)
    return 1


def cmd_replay_track(a) -> int:
    """Internal: follow a replay the way the live intake path follows a reply.

    Spawned detached by _do_replay so it outlives the media-replay process.
    Two jobs per poll tick:
    - Mirror the player's live position/pause/speed/mute into the replay's
      now_playing row (written by _do_replay, stamped with our pid) so the
      popup's progress bar moves during a replay — without this it sat frozen
      at 00:00 for the whole replay and forever after.
    - Fire the copy-mode sentence highlight (multi-clip turns with a pane).
    On observed end-of-playback we clear the row, like the live path's
    ``finally`` does; if we die uncleanly instead, the row still carries our
    pid so the store's orphan guard self-heals it on the next read.
    """
    from .intake.submit import _tmux_highlight_text, _restore_fullscreen
    sentences: list[str] = json.loads(a.sentences) if a.sentences else []
    durations: list[float] = json.loads(a.durations) if a.durations else []
    pane: str = a.pane
    highlight = bool(sentences and pane)
    # Cumulative start offset of each clip on the turn-wide timeline.
    offsets: list[float] = []
    _acc = 0.0
    for d in durations:
        offsets.append(_acc)
        _acc += d
    if highlight:
        # Ensure _tmux_highlight_text sees the right pane + a truthy TMUX.
        os.environ["TMUX_PANE"] = pane
        if not os.environ.get("TMUX"):
            os.environ["TMUX"] = "x"  # fallback: truthy, tmux resolves socket

    state = StateStore()

    def _owns(ex: dict) -> bool:
        # A newer writer (a live reply, or the next replay's tracker) may have
        # taken the row over; only touch a row that's still ours. Our own seed
        # row already carries our pid (_do_replay stamps it at spawn).
        return ex.get("writer_pid") in (None, os.getpid())

    def _mirror(snap: dict) -> None:
        try:
            np = state.get_now_playing("speech")
            if not np:
                return
            ex = np.get("extras") or {}
            if not _owns(ex):
                return
            pos = snap.get("playlist-pos")
            idx = int(pos) if pos is not None and pos >= 0 else 0
            base = offsets[idx] if idx < len(offsets) else 0.0
            ex["live_pos_s"] = base + (snap.get("time-pos") or 0.0)
            ex["live_pause"] = bool(snap.get("pause"))
            ex["live_speed"] = snap.get("speed") or 1.0
            ex["live_mute"] = bool(snap.get("mute"))
            ex["writer_pid"] = os.getpid()
            if idx < len(sentences):
                # Keeps `media current-sentence` (and popup skips) working.
                ex["current_sentence"] = sentences[idx]
                ex["current_sentence_idx"] = idx
            state.set_now_playing(
                "speech", uri=np.get("uri") or "",
                started_at=np.get("started_at") or time.time(),
                target=np.get("target") or SPEECH_TARGET.name,
                extras=ex)
        except Exception:  # noqa: BLE001
            pass

    def _finish() -> int:
        _restore_fullscreen()  # no-op unless MEDIA_HIGHLIGHT_DUMP dumped
        try:
            np = state.get_now_playing("speech")
            if np and _owns(np.get("extras") or {}):
                state.clear_now_playing("speech")
        except Exception:  # noqa: BLE001
            pass
        return 0

    # Wait for mpv to start playing the first clip — _do_replay's loadfile
    # returns immediately and there's a brief idle window before playback
    # kicks in. Without this, the tracker sees idle=True and exits before
    # the first sentence ever fires.
    for _ in range(40):  # up to ~2s
        try:
            if not bool(ipc.get_property(_sock(), "idle-active")):
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.05)

    last_pos = -1
    fail_streak = 0
    while True:
        time.sleep(0.15)
        try:
            # One batched snapshot per tick — over the phone bridge each hop
            # is slow, and this loop is per-tick anyway for the mirror.
            snap = ipc.get_properties(
                _sock(), ["idle-active", "playlist-pos", "time-pos",
                          "pause", "speed", "mute"])
        except Exception:  # noqa: BLE001
            fail_streak += 1
            if fail_streak >= 5:
                return _finish()
            continue
        fail_streak = 0
        if snap.get("idle-active"):
            # Require 2 consecutive idle readings to avoid race with playlist
            # advancement (mpv flickers idle briefly between clips).
            time.sleep(0.15)
            try:
                if bool(ipc.get_property(_sock(), "idle-active")):
                    return _finish()
            except Exception:  # noqa: BLE001
                pass
            continue
        _mirror(snap)
        pos_raw = snap.get("playlist-pos")
        pos = int(pos_raw) if pos_raw is not None else 0
        if highlight and pos != last_pos and 0 <= pos < len(sentences):
            _tmux_highlight_text(sentences[pos], first=(pos == 0))
        last_pos = pos
    return 0


def cmd_history(a) -> int:
    for r in _speech_history(a.n):
        ts = datetime.datetime.fromtimestamp(
            r.get("started_at") or 0).strftime("%H:%M")
        txt = (r.get("text") or "").replace("\n", " ").strip()[:80]
        print(f"{ts}  {txt}")
    return 0


def cmd_say(a) -> int:
    from .intake._text import strip_markdown
    from .intake._visual import (extract_visual_markers, spawn_visual,
                                 visual_enabled)
    from .intake.submit import submit_event
    text = a.text if a.text else sys.stdin.read()
    if not text.strip():
        return 0
    # `say` callers hand over prose that can carry markdown and [[visual:]]
    # markers (the same conventions hook replies use) — neither is ever worth
    # hearing. A marker still earns its picture: the same fire-and-forget
    # accompaniment the Stop hook spawns.
    raw, hint, _pre, _post = extract_visual_markers(text)
    text = strip_markdown(raw)
    if not text.strip():
        return 0
    if visual_enabled() and hint:
        try:
            spawn_visual(raw, text, hint=hint)
        except Exception:  # noqa: BLE001 — accompaniment, never speech's problem
            pass
    urgent = getattr(a, "urgent", False) or getattr(a, "supersede", False)
    metadata = {}
    if getattr(a, "supersede", False):
        # supersede implies urgent: barge in AND drop the same-session messages
        # this one interrupts/precedes, rather than letting them resume.
        metadata["supersede"] = True
    submit_event(Event(
        text=text, source=Source.CLI,
        priority=Priority.URGENT if urgent else Priority.NORMAL,
        metadata=metadata))
    return 0


# --- music subcommands -----------------------------------------------------

def _music_status_line(m: "SinkMusic", width: int, hide_idle: bool,
                       bar: bool = True) -> str:
    """One-line music progress bar from MPD status (mirrors cmd_status)."""
    st = m.status_dict()
    state = st.get("state", "stop")
    if state in ("stop", "") or not state:
        return render_status(idle=True, pos=None, dur=None, paused=None,
                             muted=None, width=width, hide_idle=hide_idle)

    def _f(key):
        try:
            return float(st[key]) if st.get(key) else None
        except (ValueError, KeyError):
            return None

    return render_status(idle=False, pos=_f("elapsed"), dur=_f("duration"),
                         paused=(state == "pause"), muted=False,
                         width=width, hide_idle=hide_idle, bar=bar)


def _music_now_label(m: "SinkMusic") -> str:
    """Current track as 'Artist — Title' (the music channel's marquee)."""
    song = m.current_song()
    if (song.get("file") or "").startswith("mpv:"):
        # mpv-routed track: MPD tags are just the bare filename; the renderer
        # has the embedded media-title (and chapter, for DJ sets/albums).
        from .sinks.music import mpv_now_props
        props = mpv_now_props()
        if props:
            label = _mpv_music_label(props)
            if label:
                return label
    title = song.get("Title") or song.get("Name") or ""
    if not title:
        title = (song.get("file") or "").rsplit("/", 1)[-1]
    artist = song.get("Artist") or ""
    return f"{artist} — {title}" if artist and title else title


def _phone_music_props() -> Optional[dict]:
    """One batched snapshot of the phone's music mpv, or None when the phone
    backend isn't configured, isn't reachable, or has nothing loaded.

    `music play --where auto` routes playout to the phone when it's the only
    listener, so the status/label/transport paths below must follow it there —
    reading Mopidy would show an idle rooms queue while the track is audibly
    playing. One `get_properties` batch = one bridge round-trip (a per-property
    read would cost several hundred ms each from this host to the phone).
    """
    from .sinks import music_local
    from .sinks import _mpv_ipc as ipc
    if not music_local.configured():
        return None
    try:
        props = ipc.get_properties(
            music_local.endpoint(),
            ["idle-active", "pause", "time-pos", "duration", "speed",
             "media-title", "chapter-metadata/by-key/title"],
            timeout=1.5)
    except (ipc.MpvIpcError, OSError):
        return None
    if props.get("idle-active") is not False:
        return None       # idle (or unknown) ⇒ the phone isn't the live backend
    return props


def _mpv_music_label(props: dict) -> str:
    """Marquee label from an mpv props snapshot (phone player or the rooms
    Mopidy-Mpv renderer): the embedded title, plus the current chapter when
    the file has chapters. Both caches key downloads by video id, so an
    unembedded file's media-title is a bare `<id>.<ext>` filename — strip the
    extension rather than showing it."""
    chap = str(props.get("chapter-metadata/by-key/title") or "").strip()
    title = str(props.get("media-title") or "").strip()
    if "." in title and " " not in title:
        title = title.rsplit(".", 1)[0]
    if chap and title:
        # Chapter first: on a ~34-col marquee the "what's playing right now"
        # part must be visible before the scroll, not after it.
        return f"{chap} · {title}"
    return chap or title


def _speed_str(props: dict) -> str:
    """Compact speed readout from an mpv props snapshot: '1.25', '' at 1.0×."""
    try:
        v = float(props.get("speed") or 1.0)
    except (TypeError, ValueError):
        return ""
    return "" if abs(v - 1.0) < 1e-3 else f"{v:g}"


def _music_now_status(m: "SinkMusic", width: int, hide_idle: bool,
                      bar: bool = True) -> tuple:
    """(status line, marquee label, speed) for whichever backend is actually
    playing: the phone's local mpv when it has a track loaded, else Mopidy.
    speed is '' at 1.0× or when the track has no speed control (MPD)."""
    props = _phone_music_props()
    if props is not None:
        line = render_status(idle=False, pos=props.get("time-pos"),
                             dur=props.get("duration"),
                             paused=bool(props.get("pause")), muted=False,
                             width=width, hide_idle=hide_idle, bar=bar)
        return line, _mpv_music_label(props), _speed_str(props)
    try:
        song = m.current_song()
    except OSError:
        song = {}
    if (song.get("file") or "").startswith("mpv:"):
        # mpv-routed rooms track: MPD reports no duration and filename-only
        # tags; the renderer knows the real position, length, and title.
        from .sinks.music import mpv_now_props
        mprops = mpv_now_props()
        if mprops:
            line = render_status(idle=False, pos=mprops.get("time-pos"),
                                 dur=mprops.get("duration"),
                                 paused=bool(mprops.get("pause")), muted=False,
                                 width=width, hide_idle=hide_idle, bar=bar)
            return line, _mpv_music_label(mprops), _speed_str(mprops)
    return (_music_status_line(m, width, hide_idle, bar),
            _music_now_label(m), "")


def _music_live_backend(m: "SinkMusic"):
    """The backend a music-channel control should hit: the phone's local mpv
    when it has a track loaded (playing or paused), else Mopidy. Mirrors
    SinkMusicRouter._observe_backend, which already makes the speech
    coordinator's duck follow the live backend — without this the popup's
    transport keys would drive an idle Mopidy while the phone plays."""
    from .sinks.music_local import SinkMusicLocal, configured
    if configured():
        loc = SinkMusicLocal()
        try:
            if loc.loaded():
                return loc
        except Exception:  # noqa: BLE001 — bridge down ⇒ phone not live
            pass
    return m


def _resolve_music_where(where: str) -> str:
    """Resolve a `--where` value to a concrete backend: 'phone' or 'rooms'.

    ``default`` follows MEDIA_MUSIC_DEFAULT_TARGET, then the speech default
    device. Explicit ``auto`` keeps the old listener-aware routing.
    """
    if where in ("local", "rooms"):
        return "rooms"
    if where == "phone":
        return "phone"
    from .sinks.music_local import configured as _local_configured
    if where in ("", "default"):
        default_target = (os.environ.get("MEDIA_MUSIC_DEFAULT_TARGET")
                          or os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET")
                          or "")
        if default_target in ("phone", "local-phone", "phone-local") and _local_configured():
            return "phone"
        if default_target in ("rooms", "local"):
            return "rooms"
        where = "auto"
    # auto
    if not _local_configured():
        return "rooms"
    from . import snapcast
    default = os.environ.get("MEDIA_MUSIC_AUTO_DEFAULT", "phone")
    try:
        others = snapcast.connected_other_clients()
    except snapcast.SnapcastError:
        return default if default in ("phone", "rooms") else "phone"
    return "rooms" if others else "phone"



def _bookmark_media_id(uri: str) -> str:
    """Stable bookmark key: YouTube id when visible, else URI/path."""
    from .sinks import music_fetch
    if vid := music_fetch.watch_id(uri or ""):
        return vid
    base = (uri or "").rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0]
    if music_fetch.watch_id(stem):
        return stem
    return uri or base


def _save_bookmark(channel: str, media_id: str, uri: str, pos_ms: int,
                   title: str = "", duration_ms: Optional[int] = None,
                   note: str = "", transcript: Optional[str] = None,
                   extras: Optional[dict] = None,
                   range_end: bool = False, slot: str = "") -> int:
    """Save a point bookmark, or let the next bookmark on that item close it.

    Pressing `b` once always creates a new point bookmark and remembers it as
    the pending range start. Pressing `bb` sends `--range-end`, which adds
    `end_pos_ms` to that pending bookmark. A later single `b` starts a fresh
    bookmark instead of closing the previous one.
    """
    st = StateStore()
    pos_ms = max(0, int(pos_ms))
    start = st.get_bookmark_pending(channel, slot=slot)
    same_item = bool(start and start.get("item_id") == media_id)
    if range_end:
        if not same_item:
            print(f"bookmark range: no matching {channel} start", file=sys.stderr)
            return 1
        a, b = int(start.get("pos_ms") or 0), pos_ms
        st.set_bookmark(
            channel=channel, media_id=start.get("media_id") or media_id, uri=uri,
            pos_ms=min(a, b), end_pos_ms=max(a, b), title=title or start.get("title"),
            duration_ms=duration_ms or start.get("duration_ms"),
            note=note or start.get("note"), transcript=transcript or start.get("transcript"),
            extras={**(start.get("extras") or {}), **(extras or {}),
                    "item_id": media_id, "range": True, "slot": slot or "default"},
        )
        st.set_bookmark_pending(channel, None, slot=slot)
        print(f"bookmarked range {fmt_time(min(a, b)/1000.0)}-{fmt_time(max(a, b)/1000.0)} {title}".rstrip())
        return 0

    bookmark_id = f"{media_id}@{pos_ms}"
    data = {
        "channel": channel, "item_id": media_id, "media_id": bookmark_id, "uri": uri,
        "pos_ms": pos_ms, "title": title or None,
        "duration_ms": duration_ms, "note": note or None,
        "transcript": transcript,
        "extras": {**(extras or {}), "slot": slot or "default"},
    }
    st.set_bookmark(
        channel=channel, media_id=bookmark_id, uri=uri, pos_ms=pos_ms,
        title=title or None, duration_ms=duration_ms, note=note or None,
        transcript=transcript,
        extras={**(extras or {}), "item_id": media_id, "slot": slot or "default"},
    )
    st.set_bookmark_pending(channel, data, slot=slot)
    print(f"bookmarked {fmt_time(pos_ms / 1000.0)} {title}".rstrip())
    return 0


def _speech_bookmark(note: str = "", range_end: bool = False,
                     slot: str = "") -> int:
    np = _now_speaking()
    if not np:
        print("media bookmark: no speech loaded", file=sys.stderr)
        return 1
    ex = np.get("extras") or {}
    text = (ex.get("text") or "").strip()
    sent = (ex.get("current_sentence") or "").strip()
    uri = np.get("uri") or f"speech:{np.get('started_at')}"
    title = sent or (" ".join(text.split())[:80] if text else "speech")
    pos = int((np.get("pause_pos_ms") or 0) or 0)
    return _save_bookmark(
        "speech", str(np.get("started_at") or uri), uri, pos,
        title=title, note=note, transcript=text or sent or None,
        extras={"pane": ex.get("pane"), "session": ex.get("session")},
        range_end=range_end, slot=slot)


def _book_bookmark(note: str = "", target: str = "", range_end: bool = False,
                   slot: str = "") -> int:
    srv = _srv()
    np = srv.book_now_playing(target=target or "")
    if np.get("idle"):
        print("media bookmark: no book loaded", file=sys.stderr)
        return 1
    uri = np.get("uri") or ""
    pos = int(np.get("position_ms") or 0)
    dur = int(np.get("duration_ms") or 0) or None
    title = uri.rsplit("/", 1)[-1] or uri
    return _save_bookmark(
        "book", _bookmark_media_id(uri), uri, pos, title=title,
        duration_ms=dur, note=note, extras={"speed": np.get("speed")},
        range_end=range_end, slot=slot)


def _music_bookmark(m: "SinkMusic", note: str = "", range_end: bool = False,
                    slot: str = "") -> int:
    b = _music_live_backend(m)
    uri = b.now_playing_uri() or ""
    if not uri and b is m:
        uri = (m.current_song() or {}).get("file") or ""
    if not uri:
        print("media bookmark: no music loaded", file=sys.stderr)
        return 1
    pos = b.position()
    if pos is None and b is m:
        try:
            pos = int(float((m.status_dict() or {}).get("elapsed") or 0) * 1000)
        except (TypeError, ValueError):
            pos = 0
    props = _phone_music_props()
    if props is None and b is m:
        from .sinks.music import mpv_now_props
        props = mpv_now_props() or {}
    dur = None
    try:
        if props and props.get("duration") is not None:
            dur = int(float(props.get("duration")) * 1000)
    except (TypeError, ValueError):
        dur = None
    _, label, _ = _music_now_status(m, width=0, hide_idle=True, bar=False)
    media_id = _bookmark_media_id(uri)
    return _save_bookmark(
        "music", media_id, uri, pos or 0, title=label or "",
        duration_ms=dur, note=note,
        extras={"backend": "phone" if b is not m else "rooms"},
        range_end=range_end, slot=slot)


def _cmd_bookmarks(limit_s: str = "", channel: Optional[str] = None,
                   json_out: bool = False) -> int:
    try:
        limit = int(limit_s or 20)
    except ValueError:
        limit = 20
    rows = StateStore().list_bookmarks(limit, channel=channel)
    if json_out:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    for bm in rows:
        title = bm.get("title") or bm.get("uri") or bm.get("media_id")
        note = f" — {bm.get('note')}" if bm.get("note") else ""
        print(f"{bm.get('channel') or '?'}  {fmt_time((bm.get('pos_ms') or 0) / 1000.0)}  {title}{note}")
    return 0


def _resume_bookmark(bm: dict) -> int:
    """Play the bookmarked item on its channel, seeking to the saved position.

    music: play (phone/rooms per auto) then seek to pos_ms.
    book:  book_play with an explicit start offset (its own fetch/resume path).
    speech: live speech can't be re-entered mid-clip, so hand back the URI.
    """
    ch = bm.get("channel")
    uri = bm.get("uri") or ""
    pos_ms = int(bm.get("pos_ms") or 0)
    if not uri:
        print("media bookmarks: bookmark has no uri", file=sys.stderr)
        return 1
    if ch == "book":
        srv = _srv()
        r = srv.book_play(uri, resume=False, start_ms=pos_ms, target="")
        if r.get("fetching"):
            print(f"⬇ {r.get('reason', 'fetching')}: {uri}")
            return 0
        if not r.get("ok", True):
            print(r.get("reason", "book play failed"), file=sys.stderr)
            return 1
        print(f"▶ {uri} (from {fmt_time(pos_ms / 1000.0)})")
        return 0
    if ch == "music":
        m = SinkMusic()
        where = _resolve_music_where("auto")
        try:
            if where == "phone":
                from .sinks.music_local import SinkMusicLocal, configured
                if not configured():
                    print("media bookmarks: phone backend not configured",
                          file=sys.stderr)
                    return 2
                SinkMusicLocal().play(uri, replace=True)
            else:
                m.play(uri, replace=True)
        except Exception as e:  # noqa: BLE001
            print(f"media bookmarks: resume failed: {e}", file=sys.stderr)
            return 1
        StateStore().set_music_intent(uri, None)
        if pos_ms > 0:
            _music_live_backend(m).seek_cur(position_ms=pos_ms)
        print(f"▶ {uri} (from {fmt_time(pos_ms / 1000.0)})")
        return 0
    # speech (and anything else): no live resume — emit the reference.
    print(uri)
    return 0


def _cmd_bookmark_pick(channel: Optional[str] = None,
                       resume: bool = True) -> int:
    rows = StateStore().list_bookmarks(500, channel=channel)
    if not rows:
        print("media bookmarks pick: no bookmarks", file=sys.stderr)
        return 1
    if not shutil.which("fzf"):
        print("media bookmarks pick: fzf not installed", file=sys.stderr)
        return 1
    lines = []
    by_key = {}
    for i, bm in enumerate(rows):
        key = str(i)
        title = bm.get("title") or bm.get("uri") or bm.get("media_id")
        searchable = " ".join(str(x or "") for x in (
            bm.get("channel"), title, bm.get("note"), bm.get("transcript"), bm.get("uri")))
        line = f"{key}	{bm.get('channel')}	{fmt_time((bm.get('pos_ms') or 0)/1000.0)}	{searchable}"
        by_key[key] = bm
        lines.append(line)
    proc = subprocess.run(
        ["fzf", "--with-nth", "2..", "--delimiter", "\t", "--prompt", "bookmark> "],
        input="\n".join(lines), text=True, capture_output=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        return proc.returncode
    bm = by_key.get(proc.stdout.split("\t", 1)[0])
    if not bm:
        return 1
    if resume:
        return _resume_bookmark(bm)
    print(bm.get("uri") or "")
    return 0


def cmd_bookmark(a) -> int:
    ch = getattr(a, "channel", "music")
    note = getattr(a, "note", "") or ""
    range_end = bool(getattr(a, "range_end", False))
    slot = getattr(a, "slot", "") or ""
    if ch == "music":
        return _music_bookmark(SinkMusic(), note, range_end=range_end, slot=slot)
    if ch == "book":
        return _book_bookmark(note, range_end=range_end, slot=slot)
    if ch == "speech":
        return _speech_bookmark(note, range_end=range_end, slot=slot)
    print("media bookmark: unsupported channel", file=sys.stderr)
    return 2


def cmd_bookmarks(a) -> int:
    if getattr(a, "pick", False):
        return _cmd_bookmark_pick(channel=getattr(a, "channel", None),
                                  resume=not getattr(a, "print_uri", False))
    return _cmd_bookmarks(getattr(a, "limit", "20") or "20",
                          channel=getattr(a, "channel", None),
                          json_out=bool(getattr(a, "json", False)))


def cmd_music(a) -> int:
    from .route import coerce_content_type, detect_content_type

    m = SinkMusic()
    if a.action in ("status", "now", "now-status"):
        # All three follow the LIVE backend (phone mpv when it has a track
        # loaded, else Mopidy). `now-status` is the popup's fused form: status
        # line + marquee label + speed readout in one spawn and one phone
        # round-trip (the speed line is '' at 1.0× / no speed control).
        line, label, spd = ("○" if a.show_idle else ""), "", ""
        try:
            line, label, spd = _music_now_status(m, a.width,
                                                 hide_idle=not a.show_idle,
                                                 bar=not a.no_bar)
        except Exception:  # noqa: BLE001 — popup must never see a traceback
            pass
        if a.action == "status":
            print(line)
        elif a.action == "now":
            print(label)
        else:
            print(line)
            print(label)
            print(spd)
        return 0
    if a.action == "bookmark":
        return _music_bookmark(m, a.uri or "", range_end=bool(getattr(a, "range_end", False)), slot=getattr(a, "slot", "") or "")
    if a.action == "bookmarks":
        return _cmd_bookmarks(a.uri or "", channel="music")
    if a.action == "play":
        if not a.uri:
            print("media music play: a URI is required", file=sys.stderr)
            return 2
        where = _resolve_music_where(getattr(a, "where", "auto"))
        ct = coerce_content_type(getattr(a, "as_type", None)) or detect_content_type(a.uri)
        if where == "phone":
            from .sinks.music_local import SinkMusicLocal, configured
            if not configured():
                print("media music play --where phone: MEDIA_MUSIC_LOCAL_ENDPOINT "
                      "is unset (phone backend not configured)", file=sys.stderr)
                return 2
            try:
                SinkMusicLocal().play(a.uri, replace=not a.add)
            except Exception as e:  # noqa: BLE001
                print(f"media music play (phone) failed: {e}", file=sys.stderr)
                return 1
            StateStore().set_music_intent(a.uri, ct.value)
            print(f"playing on phone ({ct.value}): {a.uri}")
            return 0
        m.play(a.uri, replace=not a.add)
        StateStore().set_music_intent(a.uri, ct.value)
        print(f"playing ({ct.value}): {a.uri}")
        return 0
    # Everything below is transport — route to the live backend so the keys
    # control what's actually audible (phone mpv or Mopidy).
    b = _music_live_backend(m)
    if a.action == "stop":
        b.stop()
        StateStore().clear_music_intent()
        return 0
    if a.action == "seek":
        # Timecode-aware, mirroring `book seek`: a bare value jumps absolute,
        # a signed one (+90 / -5:00) offsets. MPD seeks the current track only.
        return _do_timecode_seek(
            a.uri or "0",
            jump=lambda s: (b.seek_cur(position_ms=int(max(0.0, s) * 1000)),
                            max(0.0, s))[1],
            offset=lambda s: b.seek_relative(s),
        )
    if a.action == "speed":
        # Pitch-corrected tempo (mpv-routed tracks only — fetched YouTube in
        # rooms, the phone player). MPD/GStreamer streams have no speed knob.
        arg = (a.uri or "").strip()
        if not arg:
            cur = b.current_speed()
            print(f"{cur:.2f}×" if cur is not None
                  else "— (no speed control: no mpv track live)")
            return 0
        if arg in ("reset", "normal", "1x"):
            rate, relative = 1.0, False
        elif arg in ("up", "down"):
            # The popup's [ / ] keys: hop the shared speech/book speed ladder.
            rate = _speed_next(b.current_speed() or 1.0,
                               1 if arg == "up" else -1)
        else:
            relative = arg[0] in "+-"
            try:
                val = float(arg)
            except ValueError:
                print(f"media music speed: bad rate {arg!r} "
                      "(want 0.25–4, ±delta, or 'reset')", file=sys.stderr)
                return 2
            rate = ((b.current_speed() or 1.0) + val) if relative else val
        if not b.set_speed(rate):
            print("media music speed: no mpv track live "
                  "(MPD/GStreamer streams have no speed control)",
                  file=sys.stderr)
            return 1
        cur = b.current_speed()
        print(f"⏩ {cur:.2f}×" if cur is not None else f"⏩ {rate:.2f}×")
        return 0
    if a.action == "volume":
        b.volume_delta(int(float(a.uri or 0)))
        return 0
    if a.action == "prev" and getattr(a, "restart_first", False):
        # Popup `<`: ⏮ semantics — restart the track if we're past its start.
        if b is m:
            elapsed = lambda: (m.status_dict() or {}).get("elapsed")  # noqa: E731
        else:
            elapsed = lambda: (b.position() or 0) / 1000.0  # noqa: E731
        return _prev_with_restart(
            elapsed=elapsed,
            restart=lambda: b.seek_cur(position_ms=0),
            step_back=b.previous,
        )
    {
        "pause": b.pause, "resume": b.resume,
        "toggle": b.toggle, "next": b.next, "prev": b.previous,
    }[a.action]()
    return 0


# --- book + channel subcommands -------------------------------------------
#
# The book channel and focus/bed concurrency are orchestrated in mcp_server
# (bookmark-save on switch, playlist cursor, auto-advance). Rather than
# duplicate that here, the CLI calls those same tool functions — they're
# plain callables — and formats the result for the terminal. Imported lazily
# so frequent `media status`/`music` calls (status bar) don't pull in mcp.

def _srv():
    from . import mcp_server as srv
    return srv


def _book_stage_status(width: int, bar: bool = True) -> Optional[str]:
    try:
        from .sinks.book import read_stage_status
        st = read_stage_status()
    except Exception:
        return None
    if not st or st.get("status") not in ("copying", "playing", "error"):
        return None
    if st.get("status") == "error":
        return "! copy failed"
    total = int(st.get("total") or 0)
    copied = int(st.get("copied") or 0)
    if total <= 0:
        return "⬇ copying…"
    if not bar:
        pct = min(100, int(copied * 100 / total))
        return f"⬇ {pct}%"
    return render_status(idle=False, pos=copied / 1000.0, dur=total / 1000.0,
                         paused=False, muted=False, width=width,
                         hide_idle=False, bar=True).replace("▶", "⬇", 1)


def _book_status_line(srv, width: int, hide_idle: bool, bar: bool = True) -> str:
    staged = _book_stage_status(width, bar=bar)
    if staged:
        return staged
    np = srv.book_now_playing(target="")
    if np.get("idle"):
        return render_status(idle=True, pos=None, dur=None, paused=None,
                             muted=None, width=width, hide_idle=hide_idle)
    pos = (np.get("position_ms") or 0) / 1000.0
    dur = (np.get("duration_ms") or 0) / 1000.0 or None
    return render_status(idle=False, pos=pos, dur=dur,
                         paused=bool(np.get("paused")), muted=False,
                         width=width, hide_idle=hide_idle, bar=bar)


def _ok(result: dict) -> int:
    """Print a reason on failure; map the tool dict's ok flag to an exit code."""
    if result.get("ok") is False:
        reason = result.get("reason", "failed")
        print(f"media: {reason}", file=sys.stderr)
        return 1
    return 0


def _cmd_book_playlist(a, srv) -> int:
    pc = a.pl_cmd
    if pc == "new":
        r = srv.book_playlist_new(a.name)
        print(f"playlist {a.name!r}: "
              + ("created" if r.get("created") else "already exists"))
        return 0
    if pc == "add":
        r = srv.book_playlist_add(a.name, list(a.uris))
        print(f"playlist {a.name!r}: {r['added']} added ({r['count']} total)")
        return 0
    if pc == "play":
        r = srv.book_playlist_play(a.name, resume=not a.no_resume,
                                   target=a.target or "")
        if _ok(r):
            return 1
        title = r.get("title") or r.get("uri")
        print(f"▶ {a.name} [{r['index']}] {title}")
        return 0
    if pc == "rm":
        return _ok(srv.book_playlist_rm(a.name))
    # ls
    if a.name:
        pl = srv.book_playlist_ls(a.name)
        if _ok(pl):
            return 1
        cur = pl["cur_index"]
        if not pl["items"]:
            print(f"{a.name}: (empty)")
            return 0
        for it in pl["items"]:
            mark = "→" if it["pos"] == cur else " "
            label = it["title"] or it["uri"]
            print(f"{mark} {it['pos']:>2}  {label}")
        return 0
    lists = srv.book_playlist_ls().get("playlists", [])
    if not lists:
        print("(no book playlists)")
        return 0
    for pl in lists:
        print(f"{pl['name']:<20} {pl['count']:>3} parts  @ {pl['cur_index']}")
    return 0


def _parse_timecode(s: str) -> tuple[float, bool]:
    """Parse a position string into (seconds, relative).

    Accepts ``H:MM:SS`` / ``MM:SS`` / ``SS`` (fractions ok). A leading ``+`` or
    ``-`` makes it relative (skip ±) instead of an absolute jump:
        ``1:33:35`` → absolute 5615s   ``+90`` → +90s   ``-5:00`` → back 5min
    """
    s = s.strip()
    relative = False
    sign = 1.0
    if s[:1] == "+":
        relative, s = True, s[1:]
    elif s[:1] == "-":
        relative, sign, s = True, -1.0, s[1:]
    parts = s.split(":")
    if not parts or not all(parts):
        raise ValueError(f"bad time: {s!r}")
    try:
        secs = 0.0
        for p in parts:
            secs = secs * 60 + float(p)
    except ValueError:
        raise ValueError(f"bad time: {s!r}")
    return sign * secs, relative


def _hms(t: float) -> str:
    """``H:MM:SS`` (or ``M:SS`` under an hour) for seek/skip feedback — keeps
    seconds, unlike fmt_time which drops to ``H:MM`` at audiobook scale."""
    t = int(round(t)); h, rem = divmod(t, 3600); m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _do_timecode_seek(time_str: str, *, jump, offset,
                      force_relative: bool = False) -> int:
    """Channel-agnostic timecode seek, shared by book and music.

    Parses a timecode (``H:MM:SS`` / ``MM:SS`` / ``SS``; a leading ``+``/``-``
    makes it relative) and routes it to one of two channel callbacks:
      ``jump(secs)``   — absolute seek; may return the resulting position (s).
      ``offset(secs)`` — relative seek by ±secs.
    ``force_relative`` makes a bare, unsigned number relative instead of an
    absolute jump (the ``skip`` semantics — ``book skip 30`` means "+30s").
    Prints a one-line confirmation; returns 2 on a malformed timecode.
    """
    try:
        secs, relative = _parse_timecode(time_str)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    if force_relative:
        relative = True
    if relative:
        offset(secs)
        print(f"⏩ {'+' if secs >= 0 else '−'}{_hms(abs(secs))}")
    else:
        pos = jump(secs)
        print(f"⏱ {_hms(pos if pos is not None else secs)}")
    return 0


def _book_seek_action(srv, time_str: str, tgt: str, *,
                      force_relative: bool = False) -> int:
    """Move the book playhead, shared by ``book seek`` and ``book skip``."""
    return _do_timecode_seek(
        time_str, force_relative=force_relative,
        jump=lambda s: (srv.book_seek(position_secs=s, target=tgt)
                        .get("position_ms") or 0) / 1000,
        offset=lambda s: srv.book_skip(seconds=s, target=tgt),
    )


def cmd_book(a) -> int:
    srv = _srv()
    bc = a.book_cmd
    tgt = getattr(a, "target", "") or ""

    if bc == "playlist":
        return _cmd_book_playlist(a, srv)
    if bc == "status":
        try:
            print(_book_status_line(srv, a.width, hide_idle=not a.show_idle,
                                    bar=not a.no_bar))
        except Exception:  # noqa: BLE001 — status bar must never see a traceback
            print("○" if a.show_idle else "")
        return 0
    if bc == "now":
        np = srv.book_now_playing(target=tgt)
        if not np.get("idle"):
            print(np.get("uri") or "")
        return 0
    if bc == "meta":
        np = srv.book_now_playing(target=tgt)
        if np.get("idle"):
            print("\n\n")
            return 0
        print(np.get("title") or np.get("media_title") or np.get("uri") or "")
        try:
            from .sinks.book import read_stage_status
            staged = read_stage_status()
        except Exception:
            staged = None
        if staged and staged.get("status") == "copying":
            total = int(staged.get("total") or 0)
            copied = int(staged.get("copied") or 0)
            pct = int(copied * 100 / total) if total else 0
            print(f"copying {pct}%: {staged.get('title') or ''}")
        else:
            print(np.get("chapter_title") or "")
        print(np.get("uri") or "")
        return 0
    if bc == "bookmark":
        return _book_bookmark(getattr(a, "note", "") or "", target=tgt, range_end=bool(getattr(a, "range_end", False)), slot=getattr(a, "slot", "") or "")
    if bc == "play":
        r = srv.book_play(a.uri, resume=not a.no_resume,
                          start_ms=(a.start_ms if a.start_ms is not None else -1),
                          target=tgt, title=getattr(a, "title", "") or "")
        if r.get("fetching"):
            print(f"⬇ {r.get('reason', 'fetching')}: {r['uri']}")
            return 0
        if not r.get("ok", True):
            print(r.get("reason", "book play failed"), file=sys.stderr)
            return 1
        print(f"▶ {r['uri']} (from {fmt_time((r.get('resumed_from_ms') or 0)/1000)})")
        return 0
    if bc == "resume":
        r = srv.book_resume(target=tgt)
        return _ok(r)
    if bc == "pause":
        return _ok(srv.book_pause(target=tgt))
    if bc == "stop":
        return _ok(srv.book_stop(target=tgt))
    if bc == "next":
        return _ok(srv.book_next(target=tgt))
    if bc == "prev":
        if getattr(a, "restart_first", False):
            # Popup `<`: ⏮ semantics — restart the part if we're past its start.
            np = srv.book_now_playing(target=tgt)
            pos = None if np.get("idle") else (np.get("position_ms") or 0) / 1000.0
            return _prev_with_restart(
                elapsed=lambda: pos,
                restart=lambda: srv.book_seek(position_secs=0,
                                              target=tgt),
                step_back=lambda: srv.book_prev(target=tgt),
            )
        return _ok(srv.book_prev(target=tgt))
    if bc == "skip":
        # `skip` is relative-only sugar over the shared seek action.
        return _book_seek_action(srv, str(a.secs), tgt, force_relative=True)
    if bc == "seek":
        return _book_seek_action(srv, a.time, tgt)
    if bc == "speed":
        if a.factor in ("up", "down"):
            np = srv.book_now_playing(target=tgt)
            cur = float(np.get("speed") or 1.0) if not np.get("idle") else 1.0
            rate = _speed_next(cur, 1 if a.factor == "up" else -1)
        else:
            rate = 1.0 if a.factor == "reset" else float(a.factor)
        r = srv.book_speed(rate, target=tgt)
        print(f"speed: {r['speed']}")
        return 0
    if bc == "bed":
        return _ok(srv.book_bed(a.mode, target=tgt))
    return 2


def cmd_focus(a) -> int:
    return _ok(_srv().focus(a.channel, target=getattr(a, "target", "") or ""))


def cmd_abs_scan(a) -> int:
    from . import library
    tgt = getattr(a, "target", "") or None
    if library.trigger_abs_scan(tgt):
        print("Audiobookshelf scan started")
        return 0
    print("media abs-scan: failed to start Audiobookshelf scan", file=sys.stderr)
    return 1


def cmd_search(a) -> int:
    query = " ".join(a.query) if getattr(a, "query", None) else ""
    m = _srv()
    res = m.search(a.channel, query)
    if "error" in res:
        print(f"media search: {res['error']}", file=sys.stderr)
        return 1
    if not res.get("results"):
        print("media search: no results", file=sys.stderr)
        return 1

    if getattr(a, "lines", False):
        for r in res["results"]:
            print(f"{r['title']}\t{r['uri']}")
        return 0

    import shutil
    import subprocess

    if not shutil.which("fzf"):
        print("media search: fzf not installed", file=sys.stderr)
        for r in res["results"]:
            print(f"{r['uri']}  ({r['title']})")
        return 1

    lines = [f"{i}\t{r['title']}\t{r['uri']}" for i, r in enumerate(res["results"])]
    proc = subprocess.run(
        ["fzf", "--with-nth", "2..", "--delimiter", "\t", "--prompt", "search> "],
        input="\n".join(lines), text=True, stdout=subprocess.PIPE,
    )
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return proc.returncode

    try:
        idx = int(proc.stdout.split("\t", 1)[0])
        selected = res["results"][idx]
    except Exception:
        return 1

    print(f"Playing: {selected['title']}")
    if a.channel == "book":
        m.book_play(uri=selected['uri'], title=re.sub(r"  \[[^]]+\]$", "", selected["title"]))
    else:
        m.music_play(uri=selected['uri'])
    return 0


def cmd_channels(a) -> int:
    st = _srv().channels_status()
    print(f"focus: {st.get('focus') or '-'}   bed: {st.get('bed') or '-'}")
    mu = st.get("music") or {}
    bk = st.get("book") or {}
    print(f"music: {mu.get('uri') or '(idle)'}")
    if bk.get("idle"):
        print("book:  (idle)")
    else:
        print(f"book:  {bk.get('uri') or ''}"
              + (" [paused]" if bk.get("paused") else ""))
    return 0


# --- popup channel resolution ---------------------------------------------
#
# `prefix a` should reopen the channel you were last using, but defer to one
# that's actually playing audio. The launcher (media-popup-open) calls
# `media popup-channel` to pick the initial channel; the popup (media-popup)
# calls `media popup-channel --set <chan>` on exit to remember it.

def _popup_channel_file():
    return state_dir() / "popup-channel"


def _channel_is_playing(name: str) -> bool:
    """True when `name` is actively producing audio (not idle, not paused).

    Every probe is best-effort: a missing socket / unreachable MPD / any error
    means "not playing" rather than blowing up the launcher.
    """
    try:
        if name == "speech":
            # Require explicit False: a dead/absent socket returns None, which
            # must read as "not playing" (not as `not None` → truthy).
            return _get("idle-active") is False and _get("pause") is False
        if name == "music":
            return SinkMusic().status_dict(SPEECH_TARGET).get("state") == "play"
        if name == "book":
            # Probe the book sink directly rather than via mcp_server: importing
            # mcp_server pulls in the whole fastmcp framework (~0.47s), and this
            # runs in the popup *launcher* (`popup-channel`), before the popup
            # can even appear — so that import was the bulk of open latency.
            # SinkBook.idle/paused is what channels_status reads anyway (target
            # "local"); short-circuiting skips the paused() probe when idle.
            from .sinks import SinkBook
            b = SinkBook()
            t = Target(name="local")
            return (not b.idle(t)) and (not b.paused(t))
    except Exception:  # noqa: BLE001 — the launcher must never see a traceback
        return False
    return False


def _last_popup_channel() -> Optional[str]:
    try:
        chan = _popup_channel_file().read_text().strip()
    except OSError:
        return None
    return chan if chan in POPUP_CHANNELS else None


def cmd_popup_channel(a) -> int:
    if getattr(a, "set", None):
        if a.set in POPUP_CHANNELS:
            try:
                f = _popup_channel_file()
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(a.set)
            except OSError:
                pass
        return 0
    # Resolve: a single playing channel wins; otherwise (none or several
    # playing — e.g. a music bed under speech) fall back to the last-viewed
    # channel, then speech.
    playing = [c for c in POPUP_CHANNELS if _channel_is_playing(c)]
    if len(playing) == 1:
        print(playing[0])
    else:
        print(_last_popup_channel() or "speech")
    return 0


def cmd_doctor(a) -> int:
    """Check agent cluster health (version skew across hosts)."""
    import subprocess
    from pathlib import Path
    
    hosts = os.environ.get("MEDIA_DOCTOR_HOSTS", "p8ar red5 sp4").split()
    repos = ["agent-media", "dotfiles"]
    skewed = []
    
    # Read local hashes
    local_hashes = {}
    for r in repos:
        path = str(Path.home() / "projects" / r) if r != "dotfiles" else str(Path.home() / r)
        try:
            local_hashes[r] = subprocess.run(
                ["git", "-C", path, "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True
            ).stdout.strip()
            print(f"local {r:12}: {local_hashes[r][:7]}")
        except (OSError, subprocess.CalledProcessError):
            pass

    for host in hosts:
        print(f"checking {host}...", end="", flush=True)
        host_skewed = False
        for r, l_hash in local_hashes.items():
            try:
                # Phone uses dotfiles at ~/dotfiles, agent-media at ~/agent-media (not in projects/)
                r_path = f"~/{r}"
                res = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
                     f"git -C {r_path} rev-parse HEAD"],
                    capture_output=True, text=True, timeout=12)
                if res.returncode == 0:
                    r_hash = res.stdout.strip()
                    if r_hash != l_hash:
                        print(f" [{r} skewed: {r_hash[:7]}]", end="")
                        host_skewed = True
            except Exception:
                pass
                
        if host_skewed:
            skewed.append(host)
            print()
        else:
            print(" ok")

    d = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    ledger = d / "agent-media" / "version-skew.log"
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        if skewed:
            ledger.write_text("\n".join(skewed) + "\n")
            print(f"\nwrote {len(skewed)} skewed host(s) to ledger.")
            return 1
        else:
            ledger.unlink(missing_ok=True)
            print("\nall hosts up to date.")
            return 0
    except OSError as e:
        log.error("doctor: failed to write ledger: %s", e)
        return 1


# --- CLI -------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="media", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="one-line speech progress (for status bar)")
    s.add_argument("--width", type=int, default=12)
    s.add_argument("--show-idle", action="store_true",
                   help="emit '○' when idle instead of empty")
    s.add_argument("--no-bar", action="store_true",
                   help="show only the times (no progress bar)")
    s.add_argument("--title", nargs="?", type=_client_width, const=80,
                   default=None, metavar="CLIENT_WIDTH",
                   help="render the whole status (times + subject title) as one "
                        "background-progress bar; the title width auto-derives "
                        "from CLIENT_WIDTH — pass tmux #{client_width} so it "
                        "fits any screen (default 80; EXPERIMENTAL)")
    s.set_defaults(func=cmd_status)

    ps = sub.add_parser("popup-status",
                        help="speech status + subject label + mute-count in one "
                             "shot (3 lines) — the popup's per-refresh aggregate")
    ps.add_argument("--width", type=int, default=12)
    ps.add_argument("--show-idle", action="store_true")
    ps.add_argument("--no-bar", action="store_true")
    ps.add_argument("--act", nargs=argparse.REMAINDER, default=None,
                    help="run this media subcommand in-process before emitting "
                         "the status (fuses the popup's action+redraw into one "
                         "spawn); its stdout is prepended as a leading line. "
                         "Must be last: everything after --act is the action.")
    ps.set_defaults(func=cmd_popup_status)

    sub.add_parser("now", help="text currently being spoken").set_defaults(func=cmd_now)
    s = sub.add_parser("now-pane",
                       help="title of the pane that produced the now-playing speech")
    s.add_argument("--width", type=int,
                   help="marquee-window the title to WIDTH columns (scrolls one "
                        "column per call; used by the popup border title)")
    s.add_argument("--session-only", action="store_true")
    s.set_defaults(func=cmd_now_pane)
    sub.add_parser("goto-pane",
                   help="focus the pane that produced the now-playing speech"
                   ).set_defaults(func=cmd_goto_pane)
    sub.add_parser("goto-track",
                   help="focus the ncmpcpp pane and jump to the now-playing song"
                   ).set_defaults(func=cmd_goto_track)
    sub.add_parser("open-ncmpcpp",
                   help="open a new tmux window running ncmpcpp"
                   ).set_defaults(func=cmd_open_ncmpcpp)
    sub.add_parser("goto-book",
                   help="focus the book's mpvc-tui player pane"
                   ).set_defaults(func=cmd_goto_book)
    sub.add_parser("open-mpvc",
                   help="open a new tmux window running mpvc-tui for the book"
                   ).set_defaults(func=cmd_open_mpvc)
    p_ac = sub.add_parser("ask-context",
                          help="print a one-line 'what I'm listening to' blurb "
                               "for CHANNEL (popup `a` context seed)")
    p_ac.add_argument("--channel", default="speech",
                      choices=POPUP_CHANNELS,
                      help="speech (default) / music / book")
    p_ac.set_defaults(func=cmd_ask_context)
    p_op = sub.add_parser("open-pi",
                          help="open a fresh pi window seeded with my listening "
                               "context + a question (popup `a`)")
    p_op.add_argument("--channel", default="speech",
                      choices=POPUP_CHANNELS,
                      help="which channel's context to seed with")
    p_op.add_argument("question", nargs="?", default="",
                      help="the question to ask (context is prepended)")
    p_op.set_defaults(func=cmd_open_pi)
    sub.add_parser("speech-web",
                   help="print/open the visual canvas URL for speech"
                   ).set_defaults(func=cmd_speech_web)
    sub.add_parser("book-web",
                   help="print/open the mpvc-web browser control URL for the book"
                   ).set_defaults(func=cmd_book_web)
    sub.add_parser("music-web",
                   help="print/open the Mopidy-Iris web UI URL for music"
                   ).set_defaults(func=cmd_music_web)
    p_os = sub.add_parser("open-session",
                          help="open a window resuming a Claude Code session")
    p_os.add_argument("session", help="Claude Code session id to resume")
    p_os.set_defaults(func=cmd_open_session)
    sub.add_parser("text", help="spoken text (now-playing or latest history)").set_defaults(func=cmd_text)

    sub.add_parser("highlight-toggle",
                    help="toggle auto-highlight on/off (popup v key)"
                    ).set_defaults(func=cmd_highlight_toggle)

    sub.add_parser("highlight-now",
                    help="force highlight this turn past the keystroke-skip "
                         "until you type again (tmux prefix V)"
                    ).set_defaults(func=cmd_highlight_now)

    s = sub.add_parser("current-sentence",
                        help="active sentence (for status-line karaoke indicator)")
    s.add_argument("--width", type=int, default=80,
                    help="max chars before truncation (default 80)")
    s.set_defaults(func=cmd_current_sentence)
    sub.add_parser("toggle", help="play/pause").set_defaults(func=cmd_toggle)
    sub.add_parser("pause").set_defaults(func=cmd_pause)
    sub.add_parser("resume").set_defaults(func=cmd_resume)
    sub.add_parser("stop").set_defaults(func=cmd_stop)
    sub.add_parser("mute", help="toggle mute").set_defaults(func=cmd_mute)

    p_mp = sub.add_parser("mute-pane",
                          help="durable per-pane (or --session) speech mute")
    p_mp.add_argument("--pane",
                      help="target tmux pane id (default: $TMUX_PANE / speaking pane)")
    p_mp.add_argument("--session", action="store_true",
                      help="target the whole tmux session owning the pane")
    p_mp.add_argument("--current", action="store_true",
                      help="target the currently/last speaking pane (legacy)")
    p_mp.add_argument("--subject", action="store_true",
                      help="target the popup subject: now-playing pane, else caller")
    p_mp.add_argument("state", nargs="?", choices=["on", "off", "toggle"],
                      default="toggle")
    p_mp.set_defaults(func=cmd_mute_pane)
    sub.add_parser("mute-status",
                   help="list per-pane/session speech mutes"
                   ).set_defaults(func=cmd_mute_status)
    sub.add_parser("mute-count",
                   help="print the total number of muted panes+sessions (else nothing)"
                   ).set_defaults(func=cmd_mute_count)
    p_pm = sub.add_parser("pane-muted",
                          help="print '1' if the subject pane is muted (popup indicator)")
    p_pm.add_argument("--pane", help="pane id to check (default: popup subject)")
    p_pm.set_defaults(func=cmd_pane_muted)

    s = sub.add_parser("seek", help="seek relative seconds (+/-)")
    s.add_argument("secs", type=float)
    s.set_defaults(func=cmd_seek)

    s = sub.add_parser("volume", help="adjust volume by delta")
    s.add_argument("delta", type=int)
    s.set_defaults(func=cmd_volume)

    s = sub.add_parser(
        "speed",
        help="set playback speed: a factor, 'reset', or 'up'/'down' (±0.1)")
    s.add_argument("factor")
    s.set_defaults(func=cmd_speed)

    s = sub.add_parser("jump", help="seek to start|end of the current clip")
    s.add_argument("where", choices=("start", "end"))
    s.set_defaults(func=cmd_jump)

    s = sub.add_parser(
        "skip", help="step the reader by a sentence/paragraph (popup h/l/H/L)")
    s.add_argument("--unit", choices=("sentence", "paragraph"),
                   default="sentence")
    s.add_argument("--dir", type=int, default=1, help="-1 back, 1 forward")
    s.add_argument("--seek-fallback", type=float, default=5.0,
                   help="seconds to time-seek when there's no sentence sequence")
    s.set_defaults(func=cmd_skip)

    s = sub.add_parser("replay", help="replay the Nth most recent clip (1=latest)")
    s.add_argument("index", nargs="?", type=int, default=1)
    s.set_defaults(func=cmd_replay)

    s = sub.add_parser("replay-prev", help=argparse.SUPPRESS)  # popup < (restart-first)
    s.add_argument("--idx", type=int, default=1)
    s.set_defaults(func=cmd_replay_prev)

    sub.add_parser(
        "replay-at-cursor",
        help="replay the clip at the copy-mode cursor (popup p)"
        ).set_defaults(func=cmd_replay_at_cursor)

    s = sub.add_parser("replay-track", help=argparse.SUPPRESS)
    s.add_argument("--sentences", default="")
    s.add_argument("--pane", default="")
    s.add_argument("--durations", default="")
    s.set_defaults(func=cmd_replay_track)

    s = sub.add_parser("history", help="list recent spoken clips")
    s.add_argument("n", nargs="?", type=int, default=20)
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("say", help="speak text (stdin if no arg)")
    s.add_argument("text", nargs="?")
    s.add_argument("--urgent", action="store_true",
                   help="barge in: interrupt this session's current speech and "
                        "jump its queue, then let the interrupted message resume")
    s.add_argument("--supersede", action="store_true",
                   help="like --urgent, but DROP the same-session messages this "
                        "one interrupts/precedes instead of resuming them")
    s.set_defaults(func=cmd_say)

    s = sub.add_parser("bookmark", help="bookmark current media position")
    s.add_argument("note", nargs="?", help="optional note")
    s.add_argument("--range-end", action="store_true", help="finish a range from the last bookmark")
    s.add_argument("--slot", default="", help="named bookmark register (e.g. 1, 2) for overlapping ranges")
    s.add_argument("--channel", choices=("music", "book", "speech"), default="music")
    s.set_defaults(func=cmd_bookmark)

    s = sub.add_parser("bookmarks", help="list media bookmarks")
    s.add_argument("limit", nargs="?", default="20")
    s.add_argument("--channel", choices=("music", "book", "speech"), default=None)
    s.add_argument("--json", action="store_true")
    s.add_argument("--pick", action="store_true",
                   help="choose with fzf and resume the selected bookmark")
    s.add_argument("--print-uri", dest="print_uri", action="store_true",
                   help="with --pick: print the URI instead of resuming")
    s.set_defaults(func=cmd_bookmarks)

    s = sub.add_parser("music", help="music control via Mopidy/MPD")
    s.add_argument("action",
                   choices=("play", "pause", "resume", "stop", "toggle",
                            "next", "prev", "status", "now", "now-status",
                            "seek", "volume", "speed", "bookmark",
                            "bookmarks"))
    s.add_argument("uri", nargs="?",
                   help="for 'play': Mopidy URI (e.g. yt:https://...); "
                        "for 'seek': time H:MM:SS (absolute) or +90/-5:00 "
                        "(relative); for 'volume': ±delta; for 'speed': "
                        "rate 0.25–4 (absolute), ±delta, 'up'/'down' "
                        "(ladder), 'reset', or empty to show the current "
                        "rate; for 'bookmark': optional note; for "
                        "'bookmarks': optional limit")
    s.add_argument("--width", type=int, default=12,
                   help="for 'status': progress-bar width")
    s.add_argument("--show-idle", action="store_true",
                   help="for 'status': emit '○' when idle instead of empty")
    s.add_argument("--no-bar", action="store_true",
                   help="for 'status': show only the times (no progress bar)")
    s.add_argument("--add", action="store_true",
                   help="for 'play': queue without clearing the playlist")
    s.add_argument("--restart-first", action="store_true",
                   help="for 'prev': restart the current track if past its "
                        "start (⏮ style; grace = MEDIA_POPUP_PREV_RESTART_S)")
    s.add_argument("--as", dest="as_type", metavar="TYPE",
                   choices=("music", "audiobook", "podcast", "dj-set",
                            "ambient"),
                   help="for 'play': interruption content type "
                        "(audiobook/podcast pause instead of duck)")
    s.add_argument("--range-end", action="store_true",
                   help="for 'bookmark': finish a range from the last bookmark")
    s.add_argument("--slot", default="",
                   help="for 'bookmark': named register (e.g. 1, 2) for overlapping ranges")
    s.add_argument("--where", choices=("default", "auto", "local", "rooms", "phone"),
                   default="default",
                   help="for 'play': where to play — 'phone' downloads on the "
                        "phone (residential IP, dodges 403, offline) and plays "
                        "locally; 'rooms'/'local' use Mopidy; 'auto' picks phone "
                        "when it's the only listener (replaces play-music)")
    s.set_defaults(func=cmd_music)

    _add_book_parser(sub)

    f = sub.add_parser("focus", help="bring a channel to the front (book|music)")
    f.add_argument("channel", choices=("book", "music"))
    f.add_argument("--target", default="", help="book target; empty follows book/speech default")
    f.set_defaults(func=cmd_focus)

    search = sub.add_parser("search", help="unified search (music/book library)")
    search.add_argument("--lines", action="store_true",
                        help="print title<TAB>uri rows for an external picker")
    search.add_argument("channel", choices=("music", "book"))
    search.add_argument("query", nargs="*")
    search.set_defaults(func=cmd_search)

    abs_scan = sub.add_parser("abs-scan", help="trigger an Audiobookshelf library scan")
    abs_scan.add_argument("--target", default="",
                          help="per-target ABS library (ABS_LIBRARY_<TARGET>); empty = default")
    abs_scan.set_defaults(func=cmd_abs_scan)

    sub.add_parser("channels", help="both channels at a glance (focus, bed, what's on)"
                   ).set_defaults(func=cmd_channels)

    pc = sub.add_parser("popup-channel",
                        help="resolve (or --set) the popup's opening channel")
    pc.add_argument("--set", choices=POPUP_CHANNELS, default=None,
                    help="remember this as the last-viewed channel")
    pc.set_defaults(func=cmd_popup_channel)

    doc = sub.add_parser("doctor", help="check cluster health (version skew)")
    doc.set_defaults(func=cmd_doctor)

    return p


def _add_book_parser(sub) -> None:
    """The `media book ...` subtree — the longform/audiobook channel.

    Mirrors `media music` but with book-shaped transport (resume bookmarks,
    skip ±s, speed) and playlists. `--target rooms|local|phone` overrides where
    the book plays; empty uses MEDIA_BOOK_DEFAULT_TARGET, then speech default.
    """
    book = sub.add_parser("book", help="longform / audiobook channel")
    book.set_defaults(func=cmd_book)
    b = book.add_subparsers(dest="book_cmd", required=True)

    bp = b.add_parser("play", help="play longform audio (resumes by default)")
    bp.add_argument("uri", help="yt:https://..., http(s) stream, or file path")
    bp.add_argument("--no-resume", action="store_true",
                    help="start from the beginning, ignoring the bookmark")
    bp.add_argument("--start-ms", type=int, default=None,
                    help="explicit start offset in ms")
    bp.add_argument("--target", default="", help="rooms|local|phone")
    bp.add_argument("--title", default="", help=argparse.SUPPRESS)

    br = b.add_parser("resume", help="resume the book (reopens the last if idle)")
    br.add_argument("--target", default="")
    b.add_parser("pause", help="pause and save the place")
    b.add_parser("stop", help="stop, saving the place to resume later")
    bn = b.add_parser("next", help="next part of the active playlist")
    bn.add_argument("--target", default="")
    bpv = b.add_parser("prev", help="previous part of the active playlist")
    bpv.add_argument("--target", default="")
    bpv.add_argument("--restart-first", action="store_true",
                     help="restart the current part if past its start (⏮ style; "
                          "grace = MEDIA_POPUP_PREV_RESTART_S)")

    bk = b.add_parser("skip", help="relative ±seconds (default +30); alias of "
                                   "`seek` with a forced-relative offset")
    bk.add_argument("secs", nargs="?", type=float, default=30.0)
    bk.add_argument("--target", default="")

    bsk = b.add_parser("seek", help="jump to a time (H:MM:SS); +/- for relative")
    bsk.add_argument("time", help="absolute 1:33:35 / 93:35 / 5615, or +90 / -5:00")
    bsk.add_argument("--target", default="")

    bs = b.add_parser("speed", help="set playback speed (factor or 'reset')")
    bs.add_argument("factor")

    bbed = b.add_parser("bed", help="how music behaves under a foregrounded book")
    bbed.add_argument("mode", choices=("duck", "pause"))

    bst = b.add_parser("status", help="one-line book progress (for status bar)")
    bst.add_argument("--width", type=int, default=12)
    bst.add_argument("--show-idle", action="store_true")
    bst.add_argument("--no-bar", action="store_true")

    bnow = b.add_parser("now", help="URI of what the book channel is reading")
    bnow.add_argument("--target", default="")
    bmeta = b.add_parser("meta", help="book title/chapter/URI for popup")
    bmeta.add_argument("--target", default="")

    bbm = b.add_parser("bookmark", help="bookmark current book position")
    bbm.add_argument("note", nargs="?")
    bbm.add_argument("--range-end", action="store_true")
    bbm.add_argument("--slot", default="")
    bbm.add_argument("--target", default="")

    pl = b.add_parser("playlist", help="manage book playlists")
    pl.set_defaults(func=cmd_book)
    pls = pl.add_subparsers(dest="pl_cmd", required=True)

    pn = pls.add_parser("new", help="create an empty playlist")
    pn.add_argument("name")
    pa = pls.add_parser("add", help="append part URIs to a playlist")
    pa.add_argument("name")
    pa.add_argument("uris", nargs="+", help="one or more part URIs, in order")
    ppl = pls.add_parser("play", help="play a playlist at its remembered place")
    ppl.add_argument("name")
    ppl.add_argument("--no-resume", action="store_true",
                     help="start the playlist over from the first part")
    ppl.add_argument("--target", default="")
    pls_ls = pls.add_parser("ls", help="list playlists, or one list's parts")
    pls_ls.add_argument("name", nargs="?", default="")
    prm = pls.add_parser("rm", help="delete a playlist (keeps part bookmarks)")
    prm.add_argument("name")


def _end_opts_before_time(argv: list[str]) -> list[str]:
    """Insert ``--`` so a dash-led colon timecode parses as the seek argument.

    argparse reads a bare ``-5`` as a negative number but treats a dash-led
    *colon* timecode (``-5:00``) as an unknown option. For each seek-like
    subcommand — ``book seek``/``book skip`` and ``music seek`` — terminate
    option parsing right before a dash-led time value so a relative offset
    isn't mistaken for a flag.
    """
    a = list(argv)
    for i, tok in enumerate(a):
        seekish = ((tok in ("seek", "skip") and i > 0 and a[i - 1] == "book")
                   or (tok == "seek" and i > 0 and a[i - 1] == "music"))
        if not seekish:
            continue
        j = i + 1
        if (j < len(a) and a[j].startswith("-")
                and a[j] not in ("--", "-h", "--help", "--target")):
            a.insert(j, "--")
        break
    return a


def main(argv=None) -> int:
    from .intake._env import load_env_file
    load_env_file("media-cli")
    if argv is None:
        argv = sys.argv[1:]
    args = _build_parser().parse_args(_end_opts_before_time(argv))
    try:
        return args.func(args)
    except ipc.MpvIpcError as e:
        print(f"media: speech broker not reachable: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
