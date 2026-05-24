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
import sys
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
    print(render_status(idle=_get("idle-active"), pos=_get("time-pos"),
                        dur=_get("duration"), paused=_get("pause"),
                        muted=_get("mute"), width=a.width,
                        hide_idle=not a.show_idle))
    return 0


def cmd_now(a) -> int:
    np = _now_speaking()
    if np:
        print((np["extras"].get("text") or "").strip())
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


def _do_replay(index: int) -> int:
    rows = _speech_history(max(1, index))
    if len(rows) < index:
        print("media: no clip to replay", file=sys.stderr)
        return 1
    uri = rows[index - 1].get("uri")
    if not uri:
        return 1
    SinkSpeech().play(uri, SPEECH_TARGET)
    # A prior pause (e.g. Space while idle) would otherwise load the clip
    # paused and play nothing — force playback on.
    try:
        ipc.set_property(_sock(), "pause", False)
    except ipc.MpvIpcError:
        pass
    return 0


def cmd_replay(a) -> int:
    return _do_replay(a.index)


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

    s = sub.add_parser("replay", help="replay the Nth most recent clip (1=latest)")
    s.add_argument("index", nargs="?", type=int, default=1)
    s.set_defaults(func=cmd_replay)

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
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ipc.MpvIpcError as e:
        print(f"media: speech broker not reachable: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
