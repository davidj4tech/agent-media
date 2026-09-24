"""Always speak: conversations whose replies are never held back.

A reply can end up unheard on purpose: its pane is muted (`media mute-pane`,
the popup's `M`), or the desk toast holds it because you are looking at
another pane (intake/toast.py). Marking a conversation *priority* lifts both
for it — its replies play at once, like the pane you are looking at.

Keyed by the agent's session id (the app's thread), not a tmux pane, so the
flag follows the conversation across a resume or a move to another pane, and
outlives it (the same as a pin).

Stored in `<state_dir>/speak-priority.json` as `{"<session>": <set at, epoch>}`,
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


def _path() -> Path:
    return state_dir() / NAME


def _read() -> dict:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def priority_sessions() -> dict[str, float]:
    """Every priority conversation, session id → when it was set."""
    return {k: v for k, v in _read().items() if isinstance(v, (int, float))}


def is_priority(session: str) -> bool:
    return bool(session) and session in priority_sessions()


def set_priority(session: str, flag: bool) -> bool:
    """Mark or unmark `session`. True when that changed anything."""
    if not session:
        return False
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        try:
            rows = _read()
            if flag == (session in rows):
                return False
            if flag:
                rows[session] = round(time.time(), 3)
            else:
                rows.pop(session, None)
            tmp = path.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(rows, indent=0, sort_keys=True))
            tmp.replace(path)
        finally:
            fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
    return True
