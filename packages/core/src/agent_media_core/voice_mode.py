"""Voice mode: a thread in the app's hands-free loop (David, 28–29 Sep 2026).

The phone's `/reply` carries `voice: true` while the loop is on; the server
marks the session, and the prompt hook the same words set off
(intake/heard.py) adds a note asking for a short, spoken answer. The note is
context, not text: nothing is added to the message or the transcript. A
reply without the flag clears the mark, and a mark goes stale after
TTL_S, so a loop left on does not shape tomorrow's answers.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

TTL_S = 900.0

NOTE = (
    "The listener is talking to you hands-free: your reply is read aloud and "
    "they answer by voice. Answer in one to three short spoken sentences, "
    "plain words, no lists, tables, code or links; leave the detail in the "
    "thread for later. If a decision is needed, end with it as one question "
    "(yes or no, or two options). If they say \"more\" or \"go on\", give the "
    "next part. Before anything hard to undo (a push, a delete, a send), ask "
    "aloud and wait for a yes. For a long job, say it has started and that "
    "you'll tell them when it's done."
)


def _dir() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return state / "agent-media" / "voice-mode"


def _path(session: str) -> Path:
    return _dir() / hashlib.sha1(session.encode("utf-8")).hexdigest()


def mark(session: str, on: bool, *, now: float | None = None) -> None:
    """The listener's latest words to `session` came from the loop (on) or not."""
    session = (session or "").strip()
    if not session:
        return
    path = _path(session)
    try:
        if not on:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"session": session, "at": time.time() if now is None else now}))
    except OSError:
        pass


def active(session: str, *, now: float | None = None) -> bool:
    """Is `session` in voice mode (marked within TTL_S)?"""
    session = (session or "").strip()
    if not session:
        return False
    try:
        rec = json.loads(_path(session).read_text())
    except (OSError, ValueError):
        return False
    at = rec.get("at") if isinstance(rec, dict) else None
    now = time.time() if now is None else now
    return isinstance(at, (int, float)) and now - at <= TTL_S
