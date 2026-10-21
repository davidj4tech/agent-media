"""A conversation's voice override; absence follows the listening target."""
from __future__ import annotations

import json
import os
import threading

from . import _lock as fcntl
from ._paths import state_dir

_LOCK = threading.Lock()


def _path():
    return state_dir() / "session-voices.json"


def _read() -> dict:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def voice_for(session: str) -> str | None:
    value = _read().get(session) if session else None
    return value if isinstance(value, str) and value.startswith("edge:") else None


def set_voice(session: str, voice: str | None) -> None:
    """Set a validated Microsoft voice; None resumes following the default."""
    if not session or (voice is not None and not voice.startswith("edge:")):
        raise ValueError("a session and a Microsoft voice are required")
    path = _path()
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                rows = _read()
                if voice is None:
                    rows.pop(session, None)
                else:
                    rows[session] = voice
                tmp = path.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
                tmp.write_text(json.dumps(rows, sort_keys=True))
                tmp.replace(path)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
