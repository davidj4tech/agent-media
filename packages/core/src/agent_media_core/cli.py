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
                  hide_idle: bool = True) -> str:
    """Build the one-line status string (or '' / '○' when idle)."""
    if idle is None or idle:
        return "" if hide_idle else "○"
    icon = "⏸" if paused else "▶"
    frac = (pos / dur) if (pos and dur) else 0.0
    line = f"{icon} {fmt_mmss(pos)} {progress_bar(frac, width)} {fmt_mmss(dur)}"
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
                        width=a.width, hide_idle=not a.show_idle))
    return 0


def cmd_now(a) -> int:
    np = _now_speaking()
    if np:
        print((np["extras"].get("text") or "").strip())
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


def cmd_toggle(a) -> int:
    # If nothing is loaded, "play" means replay the latest clip (matches the
    # old popup's Space = play/pause-or-replay). Otherwise flip pause.
    if _get("idle-active"):
        return _do_replay(1)
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


def cmd_jump(a) -> int:
    """Seek to the start or end of the current clip."""
    if a.where == "start":
        ipc.command(_sock(), "seek", 0, "absolute")
    else:  # end — finish the clip (skip forward)
        ipc.command(_sock(), "seek", 100, "absolute-percent")
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
    if len(clip_uris) > 1 and len(clip_durations) == len(clip_uris):
        # Persist durations so cmd_status can compute spanning progress bar.
        StateStore().set_now_playing(
            "speech", uri=clip_uris[0], started_at=time.time(),
            target=SPEECH_TARGET.name,
            extras={"text": replay_text,
                    "total_duration_s": sum(clip_durations),
                    "clip_durations_s": clip_durations})
        # Spawn a detached highlight tracker so copy-mode follows along
        # even though _do_replay returns immediately.
        # TTS_POPUP_PANE is the original pane that opened the popup (set by
        # the tmux binding). TMUX_PANE inside display-popup is the popup's
        # own ephemeral pane, which disappears when the popup closes.
        pane = os.environ.get("TTS_POPUP_PANE") or os.environ.get("TMUX_PANE", "")
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
            return 0
        try:
            pos = int(ipc.get_property(_sock(), "playlist-pos") or 0)
        except Exception:  # noqa: BLE001
            continue
        if pos != last_pos and 0 <= pos < len(sentences):
            _tmux_highlight_text(sentences[pos])
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

def cmd_music(a) -> int:
    m = SinkMusic()
    {
        "pause": m.pause, "resume": m.resume, "stop": m.stop,
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
    s.set_defaults(func=cmd_status)

    sub.add_parser("now", help="text currently being spoken").set_defaults(func=cmd_now)
    sub.add_parser("text", help="spoken text (now-playing or latest history)").set_defaults(func=cmd_text)

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
                   choices=("pause", "resume", "stop", "toggle", "next", "prev"))
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
