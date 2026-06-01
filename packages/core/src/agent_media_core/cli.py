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


def _speech_history(n: int = 20):
    return StateStore().recent_history(sink="speech", limit=n)


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


def _spoken_pane() -> Optional[str]:
    """tmux pane id that produced the current (or most recent) speech."""
    np = _now_speaking()
    if np:
        # Actively playing: use THIS clip's source pane, or None when the
        # source had no pane (a gateway/openclaw agent, `media say`, etc.).
        # Don't fall back to history here — borrowing the last Claude pane
        # would mislabel paneless speech with a stale, wrong title.
        return (np.get("extras") or {}).get("source_pane") or None
    # Idle: keep naming whoever last spoke.
    rows = _speech_history(1)
    if rows:
        ex = rows[0].get("extras") or {}
        if isinstance(ex, str):
            try:
                ex = json.loads(ex)
            except json.JSONDecodeError:
                ex = {}
        return ex.get("source_pane") or None
    return None


def _focus_pane(pane: str) -> None:
    """Bring `pane` to the foreground — its window, then the pane itself."""
    for args in (["select-window", "-t", pane], ["select-pane", "-t", pane]):
        try:
            subprocess.run(["tmux", *args], capture_output=True)
        except Exception:  # noqa: BLE001
            pass


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
    """Focus the tmux pane that produced the now-playing (or last) speech."""
    pane = _spoken_pane()
    if pane:
        _focus_pane(pane)
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


def _do_replay(index: int) -> int:
    rows = _speech_history(max(1, index))
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
        # TTS_POPUP_PANE is the original pane that opened the popup (set by
        # the tmux binding). TMUX_PANE inside display-popup is the popup's
        # own ephemeral pane, which disappears when the popup closes.
        pane = os.environ.get("TTS_POPUP_PANE") or os.environ.get("TMUX_PANE", "")
        # If the binding left an unexpanded #{pane_id} literal, query tmux
        # for the active pane instead.
        if "#{" in pane:
            try:
                r = subprocess.run(["tmux", "display-message", "-p", "#{pane_id}"],
                                   capture_output=True, text=True)
                pane = r.stdout.strip() if r.returncode == 0 else ""
            except Exception:  # noqa: BLE001
                pane = ""
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
    return _do_replay(a.index)


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

    return p


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
