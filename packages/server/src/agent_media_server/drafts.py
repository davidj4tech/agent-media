"""Half-typed replies, held for the app's reply box (`/draft`).

Moved out of the canvas's reply.py.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import auth_abs, sessions


# --- drafts -------------------------------------------------------------------

# What is half-typed into the app's reply box, kept here rather than on the
# phone. Two reasons, and the second is why it is not localStorage: you leave
# a conversation half-answered, open another, and come back — and a reinstall
# is not supposed to lose the sentence you were in the middle of. Keyed by
# session, not by user: the same household, the same box, whichever screen it
# is read on.
#
# A draft is disposable by nature — it lives until it is sent — so this is a
# file per session and no index, written whole and read whole.
_DRAFT_LIMIT = 8192


def _drafts_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    d = root / "agent-media" / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _draft_path(session: str) -> Path:
    return _drafts_dir() / f"{session}.json"


def draft_read(session: str, bearer: str) -> tuple[bool, dict]:
    """The draft held for a session — `{"text": "", "at": 0}` when there is none."""
    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    if not auth_abs._gate(bearer)[0]:
        return False, auth_abs._gate(bearer)[1]
    try:
        data = json.loads(_draft_path(session).read_text())
    except (OSError, ValueError):
        return True, {"session": session, "text": "", "at": 0}
    return True, {"session": session,
                  "text": str(data.get("text") or "")[:_DRAFT_LIMIT],
                  "at": float(data.get("at") or 0)}


def draft_write(session: str, text: str, at, bearer: str) -> tuple[bool, dict]:
    """Hold a draft for a session, or drop it when the text is empty.

    `at` is the writer's clock, kept as given rather than stamped here: it is
    compared against another *client's* stamp — the copy the app keeps for the
    moment the canvas cannot be reached — and a server clock in the middle of
    that comparison would win or lose for the wrong reason.
    """
    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    if not auth_abs._gate(bearer)[0]:
        return False, auth_abs._gate(bearer)[1]
    text = (text or "")[:_DRAFT_LIMIT]
    try:
        at = float(at or 0) or time.time()
    except (TypeError, ValueError):
        at = time.time()
    path = _draft_path(session)
    if not text.strip():
        path.unlink(missing_ok=True)
        return True, {"session": session, "text": "", "at": at}
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({"text": text, "at": at}))
        tmp.replace(path)
    except OSError as e:
        return False, {"error": f"could not hold the draft ({e})", "status": 500}
    return True, {"session": session, "text": text, "at": at}
