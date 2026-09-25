"""A conversation's speech level: interrupt, auto, normal or quiet.

What happens to a reply when it is ready:

- **interrupt** — it plays at once, like *auto*, and at HIGH priority: a
  reply another conversation is speaking steps aside at its next sentence
  boundary and resumes afterwards (intake/submit.py `_SpeechPlaybackLock`).
- **auto** — it plays at once. The desk toast (intake/toast.py) does not hold
  it and a pane or tmux-session mute (`media mute-pane`) does not silence it.
- **normal** — it plays at once only while someone is looking at the
  conversation (its thread open in the app, or its pane at the desk);
  otherwise it waits unheard with a Play, and a toast at the desk if someone
  is there (intake/toast.py). The built-in default.
- **quiet** — it is rendered and archived but never played by itself, and
  gets no toast: it waits in the transcript unheard (`extras.held`), with a
  Play in the app.

Questions (AskUserQuestion read-outs) follow the level too, except that a
quiet conversation's question is held as a normal one's is, toast and all: it
needs an answer. An answered question's waiting read-out is dropped.

Keyed by the agent's session id (the app's thread), not a tmux pane, so the
level follows the conversation across a resume or a move to another pane, and
outlives it (the same as a pin).

The default — the level of a conversation that has none of its own — is
normal unless set (`set_default`, the app's Settings, `POST /speech/default`),
kept in `<state_dir>/speak-priority-default.json` as `{"level", "at"}`. It is
the server's, so it is every device's. Setting a conversation to the default
level clears its own, so it follows the default from then on.

Stored in `<state_dir>/speak-priority.json` as `{"<session>": {"level",
"at"}}` (a bare number is the older "always speak" flag, read as auto),
written the way the server's `_jsonmap.JsonMap` writes (temp file + rename,
under an flock on the sibling `.lock`), so the canvas and the CLI can both set
it. Read once per reply, so there is no cache.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import _lock as fcntl
from ._paths import state_dir

NAME = "speak-priority.json"
DEFAULT_NAME = "speak-priority-default.json"
LEVELS = ("interrupt", "auto", "normal", "quiet")
#: The levels whose replies are never held or muted.
SPEAKS = ("interrupt", "auto")


def _path() -> Path:
    return state_dir() / NAME


def _read(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or _path()).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _level(value) -> str | None:
    if isinstance(value, (int, float)):
        return "auto"
    if isinstance(value, dict) and value.get("level") in LEVELS:
        return value["level"]
    return None


def default_level() -> str:
    """The level of a conversation with none of its own: normal unless set."""
    return _level(_read(state_dir() / DEFAULT_NAME)) or "normal"


def levels() -> dict[str, str]:
    """Every conversation with a level of its own."""
    out = {k: _level(v) for k, v in _read().items()}
    return {k: v for k, v in out.items() if v}


def level_of(session: str) -> str:
    return (levels().get(session) or default_level()) if session else "normal"


def is_priority(session: str) -> bool:
    """Its replies are never held or muted (interrupt or auto)."""
    return level_of(session) in SPEAKS


def priority_sessions() -> dict[str, str]:
    """The conversations that always speak, session id → level."""
    return {k: v for k, v in levels().items() if v in SPEAKS}


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=0, sort_keys=True))
    tmp.replace(path)


def set_level(session: str, level: str) -> bool:
    """Set `session`'s level (the default level clears it, so it follows the
    default). True when that changed anything."""
    if not session:
        return False
    if level not in LEVELS:
        raise ValueError(f"not a speech level: {level!r}")
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        try:
            rows = _read()
            own = _level(rows.get(session))
            if level == default_level():
                if own is None:
                    return False
                rows.pop(session, None)
            elif own == level:
                return False
            else:
                rows[session] = {"level": level, "at": round(time.time(), 3)}
            _write(path, rows)
        finally:
            fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
    return True


def set_default(level: str) -> bool:
    """Set the level of every conversation with none of its own. True when
    that changed anything."""
    if level not in LEVELS:
        raise ValueError(f"not a speech level: {level!r}")
    if level == default_level():
        return False
    path = state_dir() / DEFAULT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(_path().with_suffix(".lock"), "a") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        try:
            _write(path, {"level": level, "at": round(time.time(), 3)})
        finally:
            fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
    return True


def set_priority(session: str, flag: bool) -> bool:
    """The older on/off switch: on is auto, off is normal."""
    return set_level(session, "auto" if flag else "normal")
