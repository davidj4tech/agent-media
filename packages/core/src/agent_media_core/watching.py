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

A screen turned off counts as still looking, for a while, for a thread at
the pocket level (speak_priority.py; a normal one is screen-on only). The page is hidden
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
#: The pid of the server that pinned the table (`pin`), beside it: a table
#: written by another live process is not the server's and is not read.
OWNER_NAME = "thread-watching.owner"

#: The table's path as the server resolved it when it started (`pin`); None
#: in any other process, so `publish_pinned` writes nothing there. A test's
#: stream closing after its teardown had restored the real XDG_STATE_HOME
#: once wrote `{}` over the live table under pytest's pid, and replies to an
#: open thread were held until it reopened (David, 19 Oct 2026).
_PINNED: Path | None = None


def _path() -> Path:
    return state_dir() / NAME


def pin() -> Path:
    """The server, at start: resolve the table's path now, for good, and say
    this process owns it. `publish_pinned` writes only after this."""
    global _PINNED
    _PINNED = _path()
    try:
        _PINNED.parent.mkdir(parents=True, exist_ok=True)
        owner = _PINNED.parent / OWNER_NAME
        tmp = owner.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(str(os.getpid()))
        tmp.replace(owner)
    except OSError:
        pass
    return _PINNED


def publish_pinned(sessions: dict[str, int]) -> None:
    """The server's publish: to the path pinned at its start, never one read
    from the environment now; nothing at all in a process that did not pin."""
    if _PINNED is not None:
        publish(sessions, _PINNED)


def _reset_for_tests() -> None:
    global _PINNED
    _PINNED = None


def publish(sessions: dict[str, int], path: Path | None = None) -> None:
    """Write the server's table (temp file + rename, so a reader never sees
    half of it)."""
    path = path or _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(),
                                   "sessions": {s: n for s, n in sessions.items() if n}}))
        tmp.replace(path)  # rename refuses an existing file on Windows
    except OSError:
        pass


def _alive(pid) -> bool:
    from . import procinfo

    return procinfo.alive(pid)


def _table() -> dict | None:
    """The table, when its writer is the live server: its pid alive and, when
    a live server has pinned it (`OWNER_NAME`), that server's pid. No owner
    file, or a dead owner, reads as before."""
    path = _path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not _alive(data.get("pid")):
        return None
    try:
        owner = int((path.parent / OWNER_NAME).read_text().strip())
    except (OSError, ValueError):
        owner = None
    if owner and owner != data.get("pid") and _alive(owner):
        return None
    return data


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
    tmp.replace(path)  # rename refuses an existing file on Windows
    return until


def open_sessions() -> list[str]:
    """The threads open in the app: leased as the screen went off, the
    newest lease first (a locked phone is where a headset is pressed), then
    those on screen."""
    out = [s for s, _ in sorted(_pockets().items(), key=lambda kv: -kv[1])]
    data = _table()
    if data is not None:
        sessions = data.get("sessions")
        if isinstance(sessions, dict):
            out += [s for s, n in sessions.items() if n and s not in out]
    return out


def is_open(session: str, pocketed: bool = False) -> bool:
    """Whether `session` is on screen in the app; with `pocketed`, also
    whether it was when the screen went off, not long ago."""
    if not session:
        return False
    if pocketed and session in _pockets():
        return True
    data = _table()
    if data is None:
        return False
    sessions = data.get("sessions")
    return isinstance(sessions, dict) and bool(sessions.get(session))
