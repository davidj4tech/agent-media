"""A small `{session: value}` JSON file under the state dir, safe to share.

The rested marks, the keep-open pins and the recaps agent-media wrote are
each one of these: a handful of rows keyed by session, read on every
`/targets` and written rarely, by the canvas and by the reaper's own process.
So each is the shape `archive.py` settled on — written whole and atomically
(temp file + rename), read-modify-written under a thread lock and an flock on
a sibling lock file, parsed once per (path, mtime, size) — said once here
rather than three more times.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Callable


class JsonMap:
    """`<state_dir>/<name>`: a dict of session → JSON value."""

    def __init__(self, name: str):
        self.name = name
        self._lock = threading.Lock()
        # Keyed by path as well as mtime: the state dir moves (XDG_STATE_HOME),
        # in tests every time.
        self._cache: tuple[tuple | None, dict] = (None, {})

    def path(self) -> Path:
        from agent_media_core._paths import state_dir

        return state_dir() / self.name

    def _read(self) -> dict:
        path = self.path()
        try:
            st = path.stat()
        except OSError:
            return {}
        key = (str(path), st.st_mtime_ns, st.st_size)
        if self._cache[0] == key:
            return self._cache[1]
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        self._cache = (key, data)
        return data

    def all(self) -> dict:
        """Every row. A copy (one level deep), so a caller may change it."""
        with self._lock:
            return {k: (dict(v) if isinstance(v, dict) else v)
                    for k, v in self._read().items()}

    def get(self, key: str):
        return self.all().get(key)

    def update(self, fn: Callable[[dict], bool]) -> bool:
        """Apply `fn` to the rows under both locks; write when it returns True.

        `fn` gets a private copy and changes it in place. What it returns is
        whether it changed anything, so a no-op writes nothing — which is what
        keeps a clear-on-every-send cheap.
        """
        from agent_media_core import _lock as fcntl

        path = self.path()
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path.with_suffix(".lock"), "a") as lk:
                fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
                try:
                    # Read under the file lock: another process may have
                    # written since anybody last looked.
                    rows = {k: (dict(v) if isinstance(v, dict) else v)
                            for k, v in self._read().items()}
                    if not fn(rows):
                        return False
                    tmp = path.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
                    tmp.write_text(json.dumps(rows, indent=0, sort_keys=True))
                    tmp.replace(path)
                finally:
                    fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
        return True

    def put(self, key: str, value) -> bool:
        def fn(rows: dict) -> bool:
            if rows.get(key) == value:
                return False
            rows[key] = value
            return True
        return self.update(fn)

    def drop(self, key: str) -> bool:
        """Remove `key`. Writes nothing (and takes no file lock) when absent."""
        with self._lock:
            if key not in self._read():
                return False
        return self.update(lambda rows: rows.pop(key, None) is not None)
