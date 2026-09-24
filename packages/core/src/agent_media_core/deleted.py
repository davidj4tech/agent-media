"""Deleted threads: sessions that must not come back.

Deleting a thread (`media session-delete`, agent_media_server.forget) removes
what exists now: its spoken lines, its shelf folder, its library item, its
search rows. That is not enough on its own. Every one of those is rebuilt
from somewhere else: the shelf from speech history within a minute
(`book_tracks.export_session`), the thread list and the search index from the
agent's own store (`harnesses.stored`), and the lines again if the agent ever
speaks in that session. So the id is kept here as well, and each of those
paths asks `is_deleted` before it builds anything.

Stored in `<state_dir>/deleted.json` as `{"<session>": <deleted at, epoch>}`,
written whole and atomically; small, and read far more often than written, so
it is cached by mtime.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ._paths import state_dir

_CACHE: tuple[tuple | None, dict[str, float]] = (None, {})


def _path() -> Path:
    return state_dir() / "deleted.json"


def deleted() -> dict[str, float]:
    """Every deleted session, with when."""
    global _CACHE
    path = _path()
    try:
        st = path.stat()
    except OSError:
        return {}
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _CACHE[0] == key:
        return _CACHE[1]
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    got = {str(k): float(v) for k, v in data.items()} if isinstance(data, dict) else {}
    _CACHE = (key, got)
    return got


def is_deleted(session: str) -> bool:
    return bool(session) and session in deleted()


def mark(sessions: list[str]) -> None:
    """Record `sessions` as deleted (idempotent: the first time is kept)."""
    data = dict(deleted())
    now = time.time()
    for s in sessions:
        if s:
            data.setdefault(s, now)
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    tmp.replace(path)
