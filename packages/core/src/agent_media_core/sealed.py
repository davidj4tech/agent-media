"""Sealed threads: sessions whose every trace on this host is encrypted.

Sealing a thread (agent_media_server.seal) age-encrypts what this host keeps
of it — the agent's transcript, its spoken lines and their clips, its shelf
folder — into one bundle, and takes the plaintext away. Like a deleted thread
(`deleted.py`), each of those is rebuilt from somewhere else if nothing stops
it, so the id is kept here and the same paths ask `is_sealed`. Unlike a
deleted one, a sealed thread is still listed: as a row with no title or
recap, only where it was and when it was sealed, so it can be unsealed.

Stored in `<state_dir>/sealed.json` as `{"<session>": {"at", "harness",
"cwd", "project", "bundle"}}`, written whole and atomically, cached by mtime.
Nothing in it is the thread's content: the title is in the bundle.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ._paths import state_dir

_CACHE: tuple[tuple | None, dict[str, dict]] = (None, {})


def _path() -> Path:
    return state_dir() / "sealed.json"


def sealed() -> dict[str, dict]:
    """Every sealed session, with its record."""
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
    got = {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
    _CACHE = (key, got)
    return got


def is_sealed(session: str) -> bool:
    return bool(session) and session in sealed()


def _write(data: dict[str, dict]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    tmp.replace(path)


def mark(session: str, **record) -> None:
    """Record `session` as sealed, with `record` (harness, cwd, project, bundle)."""
    data = dict(sealed())
    data[session] = {"at": round(time.time(), 3), **record}
    _write(data)


def unmark(session: str) -> None:
    data = dict(sealed())
    if data.pop(session, None) is not None:
        _write(data)
