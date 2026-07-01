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
import subprocess
import sys
import time
from typing import Optional

from ._paths import state_dir
from .sinks import _mpv_ipc as ipc
from .sinks.music import SinkMusic
from .sinks.speech import SinkSpeech, _socket_for
from .state import StateStore
from .types import Event, Source, Target

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

def _sock():
    return _socket_for(SPEECH_TARGET)


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


def cmd_status(a) -> int:
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
    print(render_status(idle=idle, pos=pos, dur=dur, paused=paused, muted=muted,
                        width=a.width, hide_idle=not a.show_idle,
                        bar=not getattr(a, "no_bar", False), speed=speed))
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
    idle, pos, dur, paused, muted, speed, _ = _speech_display_state()
    print(render_status(idle=idle, pos=pos, dur=dur, paused=paused, muted=muted,
                        width=a.width, hide_idle=not a.show_idle,
                        bar=not getattr(a, "no_bar", False), speed=speed))
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
    """Print the popup's subject-pane title (see `_subject_label`)."""
    prefix, body = _subject_label()
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


def _print_open_url(url: str) -> int:
    """Print a URL (clickable in most terminals — the reliable path, since mel
    is headless and this usually runs over SSH/tmux from a phone) and also fire
    a real browser via the stdlib webbrowser module, but only when a display or
    $BROWSER is actually present so a headless run doesn't spawn a pointless or
    hanging opener.
    """
    print(url)
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") \
            or os.environ.get("BROWSER"):
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    return 0


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
        # Exit any active copy-mode in the speaking pane.
        if pane:
            subprocess.run(["tmux", "send-keys", "-t", pane, "-X", "cancel"],
                           capture_output=True)
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
    SinkSpeech().pause(SPEECH_TARGET)
    return 0


def cmd_resume(a) -> int:
    SinkSpeech().resume(SPEECH_TARGET)
    return 0


def cmd_stop(a) -> int:
    SinkSpeech().stop(SPEECH_TARGET)
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
            SinkSpeech().stop(SPEECH_TARGET)
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
# (1.0→1.5→2.0→3.0 is +0.5,+0.5,+1.0) so a held key accelerates. Below 1.0x, fine
# flat 0.1 steps for precise control (no ladder). Symmetric for up/down. As a
# position ladder (snap off the live speed) it needs no cross-press accel state —
# each listening-mode [ / ] press is a separate `media speed` process.
_SPEED_MIN, _SPEED_MAX, _SPEED_FLAT = 0.3, 3.0, 0.1
_SPEED_RUNGS = (1.0, 1.5, 2.0, 3.0)


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
    # actual last clip before seeking it to the end.
    try:
        count = ipc.get_property(sock, "playlist-count")
        if isinstance(count, int) and count > 1:
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
        _write_nav_request(len(sentences), (np or {}).get("target") or "local")
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

    target = _nav_target(cur, n, para_idx, a.unit, direction)
    if target < 0:
        target = 0

    if playlist:
        if target >= n:
            return _seek_to_end(sock)
        try:
            ipc.set_property(sock, "playlist-pos", target)
        except ipc.MpvIpcError:
            return 1
        _force_highlight_sentence(sentences[target])
        return 0
    # Live readout: hand the jump to the reader loop (honored even while
    # paused). Key the flag by the target that's actually playing.
    _write_nav_request(target, (np or {}).get("target") or "local")
    return 0


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

    sink = SinkSpeech()
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
    StateStore().set_now_playing(
        "speech", uri=clip_uris[0], started_at=time.time(),
        target=SPEECH_TARGET.name, extras=np_extras)
    if len(clip_uris) > 1 and have_durations:
        # Spawn a detached highlight tracker so copy-mode follows along
        # even though _do_replay returns immediately.
        # TTS_POPUP_PANE is the original pane that opened the popup; TMUX_PANE
        # inside display-popup is the popup's own ephemeral pane.
        pane = _caller_pane()
        if pane and clip_sentences and len(clip_sentences) == len(clip_uris):
            # Supersede any tracker still polling from a prior replay. The
            # tracker only self-exits when the speech mpv goes idle, so
            # replaying again before the prior playlist finishes (rapid < / >
            # traversal, re-pressing r/Space) would otherwise leave the old
            # tracker running on the shared socket — it never sees "its"
            # playback end and keeps highlighting the new clip with the old
            # clip's sentences. killpg the previous one (start_new_session ⇒
            # the child's pid is its own pgid). Mirrors the per-pane pidfile
            # pattern _tmux_highlight_text uses for its clear-timer.
            import re as _re
            import signal as _signal
            _pane_safe = _re.sub(r"[^A-Za-z0-9_-]", "_", pane)
            _trk_pidfile = f"/tmp/media-replay-track-{_pane_safe}.pid"
            try:
                with open(_trk_pidfile) as _f:
                    _old_pgid = int(_f.read().strip())
                os.killpg(_old_pgid, _signal.SIGTERM)
            except (OSError, ValueError, ProcessLookupError, PermissionError):
                pass
            _trk = subprocess.Popen(
                [sys.executable, "-m", "agent_media_core.cli",
                 "replay-track",
                 "--sentences", json.dumps(clip_sentences),
                 "--pane", pane],
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
    """Internal: poll playlist-pos and fire tmux highlights during replay.

    Spawned detached by _do_replay so it outlives the media-replay process.
    """
    from .intake.submit import _tmux_highlight_text, _restore_fullscreen
    sentences: list[str] = json.loads(a.sentences)
    pane: str = a.pane
    if not sentences or not pane:
        return 0
    # Ensure _tmux_highlight_text sees the right pane + a truthy TMUX.
    os.environ["TMUX_PANE"] = pane
    if not os.environ.get("TMUX"):
        os.environ["TMUX"] = "x"  # fallback: truthy, tmux will resolve socket

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
    idle_streak = 0
    while True:
        time.sleep(0.15)
        try:
            idle = bool(ipc.get_property(_sock(), "idle-active"))
        except Exception:  # noqa: BLE001
            idle_streak += 1
            if idle_streak >= 5:
                _restore_fullscreen()  # no-op unless MEDIA_HIGHLIGHT_DUMP dumped
                return 0
            continue
        idle_streak = 0
        if idle:
            # Require 2 consecutive idle readings to avoid race with playlist
            # advancement (mpv flickers idle briefly between clips).
            time.sleep(0.15)
            try:
                if bool(ipc.get_property(_sock(), "idle-active")):
                    _restore_fullscreen()  # no-op unless MEDIA_HIGHLIGHT_DUMP dumped
                    return 0
            except Exception:  # noqa: BLE001
                pass
            continue
        try:
            pos = int(ipc.get_property(_sock(), "playlist-pos") or 0)
        except Exception:  # noqa: BLE001
            continue
        if pos != last_pos and 0 <= pos < len(sentences):
            _tmux_highlight_text(sentences[pos], first=(pos == 0))
            # Update now_playing so `media current-sentence` works during replay.
            try:
                state = StateStore()
                np = state.get_now_playing("speech")
                if np:
                    ex = np.get("extras") or {}
                    ex["current_sentence"] = sentences[pos]
                    ex["current_sentence_idx"] = pos
                    state.set_now_playing(
                        "speech", uri=np.get("uri") or "",
                        started_at=np.get("started_at") or time.time(),
                        target=np.get("target") or SPEECH_TARGET.name,
                        extras=ex)
            except Exception:  # noqa: BLE001
                pass
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
    from .intake.submit import submit_event
    text = a.text if a.text else sys.stdin.read()
    if not text.strip():
        return 0
    submit_event(Event(text=text, source=Source.CLI))
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
    title = song.get("Title") or song.get("Name") or ""
    if not title:
        title = (song.get("file") or "").rsplit("/", 1)[-1]
    artist = song.get("Artist") or ""
    return f"{artist} — {title}" if artist and title else title


def _resolve_music_where(where: str) -> str:
    """Resolve a `--where` value to a concrete backend: 'phone' or 'rooms'.

    Mirrors the old `play-music` router so it can retire: 'local'/'rooms' map
    straight to Mopidy ('rooms'); 'phone' to the phone-local backend; 'auto'
    picks phone when it's the only listener (no other room connected to the
    snapserver) and rooms otherwise. On an unreachable snapserver, fall back to
    MEDIA_MUSIC_AUTO_DEFAULT (default 'phone' — offline-capable, survives a hub
    hiccup). When the phone backend isn't configured, 'auto' always means rooms.
    """
    if where in ("local", "rooms"):
        return "rooms"
    if where == "phone":
        return "phone"
    # auto
    from .sinks.music_local import configured as _local_configured
    if not _local_configured():
        return "rooms"
    from . import snapcast
    default = os.environ.get("MEDIA_MUSIC_AUTO_DEFAULT", "phone")
    try:
        others = snapcast.connected_other_clients()
    except snapcast.SnapcastError:
        return default if default in ("phone", "rooms") else "phone"
    return "rooms" if others else "phone"


def cmd_music(a) -> int:
    from .route import coerce_content_type, detect_content_type

    m = SinkMusic()
    if a.action == "status":
        try:
            print(_music_status_line(m, a.width, hide_idle=not a.show_idle,
                                     bar=not a.no_bar))
        except Exception:  # noqa: BLE001 — popup must never see a traceback
            print("○" if a.show_idle else "")
        return 0
    if a.action == "now":
        try:
            print(_music_now_label(m))
        except Exception:  # noqa: BLE001
            pass
        return 0
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
    if a.action == "stop":
        m.stop()
        StateStore().clear_music_intent()
        return 0
    if a.action == "seek":
        # Timecode-aware, mirroring `book seek`: a bare value jumps absolute,
        # a signed one (+90 / -5:00) offsets. MPD seeks the current track only.
        return _do_timecode_seek(
            a.uri or "0",
            jump=lambda s: (m.seek_cur(position_ms=int(max(0.0, s) * 1000)),
                            max(0.0, s))[1],
            offset=lambda s: m.seek_relative(s),
        )
    if a.action == "volume":
        m.volume_delta(int(float(a.uri or 0)))
        return 0
    if a.action == "prev" and getattr(a, "restart_first", False):
        # Popup `<`: ⏮ semantics — restart the track if we're past its start.
        return _prev_with_restart(
            elapsed=lambda: (m.status_dict() or {}).get("elapsed"),
            restart=lambda: m.seek_cur(position_ms=0),
            step_back=m.previous,
        )
    {
        "pause": m.pause, "resume": m.resume,
        "toggle": m.toggle, "next": m.next, "prev": m.previous,
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


def _book_status_line(srv, width: int, hide_idle: bool, bar: bool = True) -> str:
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
        jump=lambda s: (srv.book_seek(position_secs=s, target=tgt or "local")
                        .get("position_ms") or 0) / 1000,
        offset=lambda s: srv.book_skip(seconds=s, target=tgt or "local"),
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
    if bc == "play":
        r = srv.book_play(a.uri, resume=not a.no_resume,
                          start_ms=(a.start_ms if a.start_ms is not None else -1),
                          target=tgt)
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
        return _ok(srv.book_pause(target=tgt or "local"))
    if bc == "stop":
        return _ok(srv.book_stop(target=tgt or "local"))
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
                                              target=tgt or "local"),
                step_back=lambda: srv.book_prev(target=tgt),
            )
        return _ok(srv.book_prev(target=tgt))
    if bc == "skip":
        # `skip` is relative-only sugar over the shared seek action.
        return _book_seek_action(srv, str(a.secs), tgt, force_relative=True)
    if bc == "seek":
        return _book_seek_action(srv, a.time, tgt)
    if bc == "speed":
        rate = 1.0 if a.factor == "reset" else float(a.factor)
        r = srv.book_speed(rate, target=tgt or "local")
        print(f"speed: {r['speed']}")
        return 0
    if bc == "bed":
        return _ok(srv.book_bed(a.mode, target=tgt or "local"))
    return 2


def cmd_focus(a) -> int:
    return _ok(_srv().focus(a.channel, target="local"))


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
            st = _srv().channels_status().get("book") or {}
            return (not st.get("idle")) and (not st.get("paused"))
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
    sub.add_parser("now-pane",
                   help="title of the pane that produced the now-playing speech"
                   ).set_defaults(func=cmd_now_pane)
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
    s.add_argument("--sentences", required=True)
    s.add_argument("--pane", required=True)
    s.set_defaults(func=cmd_replay_track)

    s = sub.add_parser("history", help="list recent spoken clips")
    s.add_argument("n", nargs="?", type=int, default=20)
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("say", help="speak text (stdin if no arg)")
    s.add_argument("text", nargs="?")
    s.set_defaults(func=cmd_say)

    s = sub.add_parser("music", help="music control via Mopidy/MPD")
    s.add_argument("action",
                   choices=("play", "pause", "resume", "stop", "toggle",
                            "next", "prev", "status", "now", "seek", "volume"))
    s.add_argument("uri", nargs="?",
                   help="for 'play': Mopidy URI (e.g. yt:https://...); "
                        "for 'seek': time H:MM:SS (absolute) or +90/-5:00 "
                        "(relative); for 'volume': ±delta")
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
    s.add_argument("--where", choices=("auto", "local", "rooms", "phone"),
                   default="auto",
                   help="for 'play': where to play — 'phone' downloads on the "
                        "phone (residential IP, dodges 403, offline) and plays "
                        "locally; 'rooms'/'local' use Mopidy; 'auto' picks phone "
                        "when it's the only listener (replaces play-music)")
    s.set_defaults(func=cmd_music)

    _add_book_parser(sub)

    f = sub.add_parser("focus", help="bring a channel to the front (book|music)")
    f.add_argument("channel", choices=("book", "music"))
    f.set_defaults(func=cmd_focus)

    sub.add_parser("channels", help="both channels at a glance (focus, bed, what's on)"
                   ).set_defaults(func=cmd_channels)

    pc = sub.add_parser("popup-channel",
                        help="resolve (or --set) the popup's opening channel")
    pc.add_argument("--set", choices=POPUP_CHANNELS, default=None,
                    help="remember this as the last-viewed channel")
    pc.set_defaults(func=cmd_popup_channel)

    return p


def _add_book_parser(sub) -> None:
    """The `media book ...` subtree — the longform/audiobook channel.

    Mirrors `media music` but with book-shaped transport (resume bookmarks,
    skip ±s, speed) and playlists. `--target rooms|local` overrides where the
    book plays; empty uses MEDIA_BOOK_DEFAULT_TARGET.
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
    bp.add_argument("--target", default="", help="rooms|local")

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
