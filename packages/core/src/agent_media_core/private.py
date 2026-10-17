"""Private threads: sessions whose content stays in the thread.

The mark is agent-config's (`agent-private`, docs/2026-09-07-private-sessions
in agent-config): a file `~/.local/state/agent/private.d/<session>` holding
`{"cwd", "started"[, "ended"]}`, written by its SessionStart hook under
`AGENT_PRIVATE=1` and, for a chat started as a private persona's private
render, by agent-media (personas.py, contract §6.24). agent-private-guard
keeps marked sessions from the hooks it wraps; this is the same question for
agent-media's own readers.

A private thread still speaks and is listed, but nothing of it is rewritten
by a model, suggested from, drawn, recapped, titled by a model, indexed for
search, fed to the shelf or a podcast feed, or summed up in a catch-up —
each of those asks `hidden`, which is sealed or private. It is sealed when it
ends, or after `MEDIA_PRIVATE_IDLE` seconds with no turn (default two hours;
reap.py).

`AGENT_PRIVATE_DIR` moves the markers (tests).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def marker_dir() -> Path:
    raw = (os.environ.get("AGENT_PRIVATE_DIR") or "").strip()
    return Path(os.path.expanduser(raw or "~/.local/state/agent/private.d"))


def is_private(session: str) -> bool:
    return bool(session) and "/" not in session and (marker_dir() / session).is_file()


def hidden(session: str) -> bool:
    """Sealed or private: a thread whose content no reader may take elsewhere."""
    from .sealed import is_sealed

    return is_sealed(session) or is_private(session)


def mark(session: str, cwd: str = "") -> bool:
    """Mark `session` private, as `agent-private mark` does; an existing mark
    is kept. False when the id is unusable or the file cannot be written."""
    if not session or "/" in session:
        return False
    path = marker_dir() / session
    if path.exists():
        return True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"cwd": cwd, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "by": "agent-media"}, fh)
    except FileExistsError:
        return True
    except OSError:
        return False
    return True


def marked() -> list[str]:
    """Every marked session id."""
    try:
        return sorted(p.name for p in marker_dir().iterdir() if p.is_file())
    except OSError:
        return []
