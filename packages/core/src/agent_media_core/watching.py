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

A screen turned off counts as still looking, for a while. The page is hidden
then too, so the stream closes, but a locked phone is not a look away: you
sent a message, pocketed the phone, and want the answer spoken. So the app,
when its page hides with the screen off (not for another app), leases the
thread open (`POST /session/pocket`) and gives the lease back when it shows
again. `<state_dir>/thread-pocketed.json` is `{"<session>": <until, epoch s>}`;
a lease runs out after MEDIA_POCKET_S (default 1800), so a thread left open
on the nightstand does not talk at 3 am (David, 28 Sep 2026).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ._paths import state_dir

NAME = "thread-watching.json"
POCKET_NAME = "thread-pocketed.json"


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


def pocket_s() -> int:
    try:
        return int(os.environ.get("MEDIA_POCKET_S", "1800"))
    except ValueError:
        return 1800


def _pockets() -> dict[str, float]:
    """The unexpired leases."""
    try:
        data = json.loads((state_dir() / POCKET_NAME).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    now = time.time()
    return {s: t for s, t in data.items()
            if isinstance(t, (int, float)) and t > now}


def pocket(session: str, on: bool) -> float:
    """Lease `session` open for `pocket_s()` (the screen went off on it), or
    give its lease back (`on` false: the page shows again). Answers the
    lease's end, 0 for none. Expired leases are dropped on the way."""
    leases = _pockets()
    leases.pop(session, None)
    until = time.time() + pocket_s() if on and pocket_s() > 0 else 0.0
    if until:
        leases[session] = until
    path = state_dir() / POCKET_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(leases))
    tmp.rename(path)
    return until


def is_open(session: str) -> bool:
    """Whether `session` is on screen in the app, or was when the screen
    went off, not long ago."""
    if not session:
        return False
    if session in _pockets():
        return True
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or not _alive(data.get("pid")):
        return False
    sessions = data.get("sessions")
    return isinstance(sessions, dict) and bool(sessions.get(session))
