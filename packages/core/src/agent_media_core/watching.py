"""Which threads are open in the app right now.

The app holds a thread's event stream (`GET /threads/{session}/events`, the
server's thread_events.py) only while that thread is on screen: it closes it
when the page is hidden. So "has a subscriber" is "someone is looking at it",
and a Normal reply (speak_priority.py) from a thread being looked at plays at
once instead of waiting for a tap.

The server publishes its subscriber table to `<state_dir>/thread-watching.json`
as `{"pid", "sessions": {"<session>": <connections>}}` whenever it changes; the
Stop hook, another process, reads it. The pid is the server's: a server that
died with streams open left a table nobody is watching, so a dead pid reads as
nothing open.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ._paths import state_dir

NAME = "thread-watching.json"


def _path() -> Path:
    return state_dir() / NAME


def publish(sessions: dict[str, int]) -> None:
    """Write the server's table (temp file + rename, so a reader never sees
    half of it)."""
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(),
                                   "sessions": {s: n for s, n in sessions.items() if n}}))
        tmp.rename(path)
    except OSError:
        pass


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return True


def is_open(session: str) -> bool:
    """Whether `session` is on screen in the app."""
    if not session:
        return False
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or not _alive(data.get("pid")):
        return False
    sessions = data.get("sessions")
    return isinstance(sessions, dict) and bool(sessions.get(session))
