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
import subprocess
import sys
import time
from typing import Optional

from .sinks import _mpv_ipc as ipc
from .sinks.music import SinkMusic
from .sinks.speech import SinkSpeech, _socket_for
from .state import StateStore
from .types import Event, Source, Target


SPEECH_TARGET = Target("local")


# --- pure helpers (unit-tested) -------------------------------------------

def fmt_mmss(secs: Optional[float]) -> str:
    if secs is None:
        return "--:--"
    secs = max(0, int(secs))
    return f"{secs // 60:02d}:{secs % 60:02d}"


def progress_bar(frac: float, width: int = 12) -> str:
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    return "█" * filled + "░" * (width - filled)


def render_status(*, idle: Optional[bool], pos: Optional[float],
                  dur: Optional[float], paused: Optional[bool],
                  muted: Optional[bool], width: int = 12,
                  hide_idle: bool = True, bar: bool = True) -> str:
    """Build the one-line status string (or '' / '○' when idle).

    With bar=False, the progress bar is dropped and only the times remain
    (`▶ 00:30 / 02:00`) — used by the popup, which shows just the clock.
    """
    if idle is None or idle:
        return "" if hide_idle else "○"
    icon = "⏸" if paused else "▶"
    if bar:
        frac = (pos / dur) if (pos and dur) else 0.0
        line = f"{icon} {fmt_mmss(pos)} {progress_bar(frac, width)} {fmt_mmss(dur)}"
    else:
        line = f"{icon} {fmt_mmss(pos)} / {fmt_mmss(dur)}"
    if muted:
        line += " [M]"
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
    # Exclude "Claude is waiting" notif clips: they're alerts, not responses,
    # and shouldn't appear when traversing past TTS (popup < / >, r, replay).
    # Over-fetch so filtering still leaves n real responses to step through;
    # over-fetch harder when also scoping to one tmux session, since other
    # sessions' clips interleave and would otherwise crowd out the buffer.
    fetch = max(n * 4, n + 50)
    if session:
        fetch = max(fetch, 400)
    rows = StateStore().recent_history(sink="speech", limit=fetch)
    rows = [r for r in rows
            if not (isinstance(r.get("extras"), dict)
                    and r["extras"].get("kind") == "notif")]
    if session:
        # Scope traversal to one tmux session's clips. Rows predate the
        # source_tmux_session field (or came from a non-tmux source) carry no
        # session tag and are excluded rather than leaking across sessions.
        rows = [r for r in rows
                if isinstance(r.get("extras"), dict)
                and r["extras"].get("source_tmux_session") == session]
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
    """The tmux session the popup's < / > traversal should stay within.

    Follows what you're hearing: the now-playing clip's tmux session if one is
    playing; otherwise the session of the pane that opened the popup
    (TTS_POPUP_PANE, exported by the tmux binding). Returns None when neither
    resolves — callers then fall back to unscoped (all-session) history.
    """
    np = StateStore().get_now_playing("speech")
    ex = (np or {}).get("extras") or {}
    if isinstance(ex, str):
        try:
            ex = json.loads(ex)
        except json.JSONDecodeError:
            ex = {}
    sess = ex.get("source_tmux_session")
    if sess:
        return sess
    sess = _tmux_session_for_pane(os.environ.get("TTS_POPUP_PANE", ""))
    return sess or None


def _caller_pane() -> str:
    """The pane that opened the popup (TTS_POPUP_PANE), resolving an
    unexpanded ``#{pane_id}`` literal by asking tmux for the active pane.
    Inside ``display-popup`` TMUX_PANE is the popup's own ephemeral pane, so
    TTS_POPUP_PANE is the one we want; it falls back to TMUX_PANE otherwise."""
    pane = os.environ.get("TTS_POPUP_PANE") or os.environ.get("TMUX_PANE", "")
    if "#{" in pane:
        try:
            r = subprocess.run(["tmux", "display-message", "-p", "#{pane_id}"],
                               capture_output=True, text=True)
            pane = r.stdout.strip() if r.returncode == 0 else ""
        except Exception:  # noqa: BLE001
            pane = ""
    return pane


# --- speech subcommands ----------------------------------------------------

def cmd_status(a) -> int:
    idle = _get("idle-active")
    pos = _get("time-pos")
    dur = _get("duration")
    if not idle:
        np = _now_speaking()
        if np:
            ex = np.get("extras") or {}
            total = ex.get("total_duration_s")
            if total:
                clip_durs = ex.get("clip_durations_s")
                if clip_durs:
                    # Replay path: compute offset from mpv playlist-pos so
                    # no background thread is needed to track clip advances.
                    try:
                        ppos = max(0, int(ipc.get_property(_sock(), "playlist-pos") or 0))
                    except Exception:  # noqa: BLE001
                        ppos = 0
                    offset = sum(clip_durs[:ppos])
                else:
                    offset = ex.get("clip_offset_s") or 0.0
                pos = offset + (pos or 0.0)
                dur = total
    print(render_status(idle=idle, pos=pos, dur=dur,
                        paused=_get("pause"), muted=_get("mute"),
                        width=a.width, hide_idle=not a.show_idle,
                        bar=not getattr(a, "no_bar", False)))
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


def cmd_now_pane(a) -> int:
    """Title of the tmux pane that produced the speech.

    The popup shows this so the marquee names *which* pane is talking. When
    nothing is playing, falls back to the pane of the most recent clip so the
    marquee keeps showing who last spoke (rather than reverting to a generic
    label). Prints nothing when no pane was ever captured.
    """
    pane = _spoken_pane()
    if not pane:
        return 0
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{pane_title}"],
            capture_output=True, text=True)
        if r.returncode == 0:
            print(r.stdout.strip())
    except Exception:  # noqa: BLE001 — popup must never see a traceback
        pass
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


def cmd_open_session(a) -> int:
    """Open a new tmux window resuming the given Claude Code session.

    The popup calls this after `goto-pane` reports a closed pane (rc 3) and
    the user confirms — it brings the conversation back as `claude --resume`.
    """
    sid = (getattr(a, "session", "") or "").strip()
    if not sid:
        return 1
    try:
        subprocess.run(["tmux", "new-window", f"claude --resume {sid}"],
                       capture_output=True)
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


def cmd_toggle(a) -> int:
    # If nothing is loaded, "play" means replay a clip (matches the old
    # popup's Space = play/pause-or-replay). Prefer the most recent clip from
    # the *active* pane (the one that opened the popup), so Space-while-idle
    # replays "what this pane just said"; fall back to the latest overall.
    # Otherwise flip pause.
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


def cmd_speed(a) -> int:
    ipc.set_property(_sock(), "speed",
                     1.0 if a.factor == "reset" else float(a.factor))
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
    # Play first clip (replace), then queue the rest — mpv plays them
    # sequentially as a playlist so the full response replays intact.
    sink.play(clip_uris[0], SPEECH_TARGET)
    for extra_uri in clip_uris[1:]:
        sink.queue(extra_uri, SPEECH_TARGET)
    # "Replay" means "I want to hear this now": clear a lingering pause/mute.
    try:
        ipc.set_property(_sock(), "pause", False)
        ipc.set_property(_sock(), "mute", False)
    except ipc.MpvIpcError:
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
    # Carry the clip's tmux session forward so the next < / > press anchors to
    # the same session (keeps the traversal scope stable across the walk).
    src_sess = ex.get("source_tmux_session")
    if src_sess:
        np_extras["source_tmux_session"] = src_sess
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
            subprocess.Popen(
                [sys.executable, "-m", "agent_media_core.cli",
                 "replay-track",
                 "--sentences", json.dumps(clip_sentences),
                 "--pane", pane],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    return 0


def cmd_replay(a) -> int:
    # Scope < / > / r traversal to the current tmux session's clips.
    return _do_replay(a.index, session=_anchor_session())


def cmd_replay_at_cursor(a) -> int:
    """Replay the spoken clip at/just-above the copy-mode cursor (popup `p`).

    "The clip in the sequence before the cursor": capture the caller pane's
    text down to the cursor row, then play the most recent clip whose search
    anchor appears in it — clips below the cursor never appear in the capture,
    so they're excluded for free, and most-recent-first picks the nearest
    preceding utterance. Reuses `_anchor_for` so a clip that the auto-highlight
    can land on is exactly one this can match. If the pane isn't scrolled into
    copy-mode there's no cursor, so it falls back to "replay what this pane
    just said" (the latest clip from this pane).
    """
    from .intake.submit import _anchor_for

    pane = _caller_pane()
    if not pane:
        print("media: no caller pane", file=sys.stderr)
        return 1
    # Match against the caller pane's own scrollback, so scope history to that
    # pane's session (not the now-playing one, which may be elsewhere).
    sess = _tmux_session_for_pane(pane) or _anchor_session()

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

    # Not scrolled into copy-mode → no cursor to anchor on; replay this pane's
    # latest clip (matches Space's "play what this pane just said").
    if in_mode.strip() != "1" or not cur_y.strip().isdigit():
        idx = _history_index_for_pane(pane)
        if idx is None:
            print("media: this pane has no spoken clip", file=sys.stderr)
            return 1
        return _do_replay(idx, session=sess)

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

    rows = _speech_history(200, session=sess)
    for i, row in enumerate(rows, start=1):
        anchor = _anchor_for(row.get("text") or "")
        if anchor and anchor in captured:
            preview = " ".join((row.get("text") or "").split())
            if len(preview) > 60:
                preview = preview[:57] + "…"
            subprocess.run(["tmux", "display-message", f"♪ {preview}"],
                           capture_output=True)
            return _do_replay(i, session=sess)

    subprocess.run(
        ["tmux", "display-message", "⊘ no spoken clip above cursor"],
        capture_output=True)
    print("media: no spoken clip above cursor", file=sys.stderr)
    return 1


def cmd_replay_track(a) -> int:
    """Internal: poll playlist-pos and fire tmux highlights during replay.

    Spawned detached by _do_replay so it outlives the media-replay process.
    """
    from .intake.submit import _tmux_highlight_text
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
                return 0
            continue
        idle_streak = 0
        if idle:
            # Require 2 consecutive idle readings to avoid race with playlist
            # advancement (mpv flickers idle briefly between clips).
            time.sleep(0.15)
            try:
                if bool(ipc.get_property(_sock(), "idle-active")):
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
        m.play(a.uri, replace=not a.add)
        ct = coerce_content_type(getattr(a, "as_type", None)) or detect_content_type(a.uri)
        StateStore().set_music_intent(a.uri, ct.value)
        print(f"playing ({ct.value}): {a.uri}")
        return 0
    if a.action == "stop":
        m.stop()
        StateStore().clear_music_intent()
        return 0
    if a.action == "seek":
        m.seek_relative(float(a.uri or 0))
        return 0
    if a.action == "volume":
        m.volume_delta(int(float(a.uri or 0)))
        return 0
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
        print(f"▶ {r['uri']} (from {fmt_mmss((r['resumed_from_ms'] or 0)/1000)})")
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
        return _ok(srv.book_prev(target=tgt))
    if bc == "skip":
        return _ok(srv.book_skip(seconds=a.secs, target=tgt or "local"))
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
    s.set_defaults(func=cmd_status)

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
    p_os = sub.add_parser("open-session",
                          help="open a window resuming a Claude Code session")
    p_os.add_argument("session", help="Claude Code session id to resume")
    p_os.set_defaults(func=cmd_open_session)
    sub.add_parser("text", help="spoken text (now-playing or latest history)").set_defaults(func=cmd_text)

    sub.add_parser("highlight-toggle",
                    help="toggle auto-highlight on/off (popup v key)"
                    ).set_defaults(func=cmd_highlight_toggle)

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

    s = sub.add_parser("seek", help="seek relative seconds (+/-)")
    s.add_argument("secs", type=float)
    s.set_defaults(func=cmd_seek)

    s = sub.add_parser("volume", help="adjust volume by delta")
    s.add_argument("delta", type=int)
    s.set_defaults(func=cmd_volume)

    s = sub.add_parser("speed", help="set playback speed (factor or 'reset')")
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
                        "for 'seek': ±seconds; for 'volume': ±delta")
    s.add_argument("--width", type=int, default=12,
                   help="for 'status': progress-bar width")
    s.add_argument("--show-idle", action="store_true",
                   help="for 'status': emit '○' when idle instead of empty")
    s.add_argument("--no-bar", action="store_true",
                   help="for 'status': show only the times (no progress bar)")
    s.add_argument("--add", action="store_true",
                   help="for 'play': queue without clearing the playlist")
    s.add_argument("--as", dest="as_type", metavar="TYPE",
                   choices=("music", "audiobook", "podcast", "dj-set",
                            "ambient"),
                   help="for 'play': interruption content type "
                        "(audiobook/podcast pause instead of duck)")
    s.set_defaults(func=cmd_music)

    _add_book_parser(sub)

    f = sub.add_parser("focus", help="bring a channel to the front (book|music)")
    f.add_argument("channel", choices=("book", "music"))
    f.set_defaults(func=cmd_focus)

    sub.add_parser("channels", help="both channels at a glance (focus, bed, what's on)"
                   ).set_defaults(func=cmd_channels)

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

    bk = b.add_parser("skip", help="seek ±seconds (default +30)")
    bk.add_argument("secs", nargs="?", type=float, default=30.0)

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


def main(argv=None) -> int:
    from .intake._env import load_env_file
    load_env_file("media-cli")
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ipc.MpvIpcError as e:
        print(f"media: speech broker not reachable: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
