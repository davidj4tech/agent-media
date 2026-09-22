"""The AskUserQuestion a Claude Code session has on screen, kept as data.

Claude Code writes an AskUserQuestion to its transcript only once it has been
answered, so while the modal is up the only complete copy of what was asked —
every question, every option, which ones are multi-select — is the tool input
the PreToolUse hook is handed. The hook keeps it here, one file per session,
and the server reads it back to draw the question on the phone and to work
out which keys answer it (agent_media_server.sessions). The screen stays the
proof that the question is still up: a file whose question is not on screen
is simply not used, so a stale one does no harm.

    <state_dir>/asks/<session>.json
    {"session", "tool_use_id", "at", "questions": [{"question", "header",
     "multiSelect", "options": [{"label", "description"}]}]}
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from ._paths import state_dir

_SESSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z_.-]{0,127}")


def _dir() -> Path:
    return state_dir() / "asks"


def _path(session: str) -> Path | None:
    if not session or not _SESSION.fullmatch(session):
        return None
    return _dir() / f"{session}.json"


def questions_of(tool_input: dict) -> list[dict]:
    """AskUserQuestion's `questions`, cleaned to the fields a screen needs."""
    out = []
    for q in (tool_input or {}).get("questions") or []:
        if not isinstance(q, dict):
            continue
        text = str(q.get("question") or "").strip()
        if not text:
            continue
        out.append({"question": text,
                    "header": str(q.get("header") or "").strip(),
                    "multiSelect": bool(q.get("multiSelect")),
                    "options": [{"label": str(o.get("label") or "").strip(),
                                 "description": str(o.get("description") or "").strip()}
                                for o in q.get("options") or []
                                if isinstance(o, dict) and str(o.get("label") or "").strip()]})
    return out


def record(session: str, tool_use_id: str, tool_input: dict) -> bool:
    """Keep the question `session` is about to put up. Whether it was kept."""
    path = _path(session)
    qs = questions_of(tool_input)
    if path is None or not qs:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"session": session, "tool_use_id": tool_use_id or "",
                                   "at": round(time.time(), 3), "questions": qs}))
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def read(session: str) -> dict | None:
    """The question `session` last put up, or None."""
    path = _path(session)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("questions") else None


def clear(session: str, tool_use_id: str = "") -> None:
    """Forget it once answered (only that one, when `tool_use_id` is given)."""
    path = _path(session)
    if path is None:
        return
    if tool_use_id:
        cur = read(session)
        if cur and cur.get("tool_use_id") and cur["tool_use_id"] != tool_use_id:
            return
    try:
        path.unlink()
    except OSError:
        pass
