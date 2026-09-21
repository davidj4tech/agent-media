"""Resting sessions: the ones the reaper closed, and the recap it left them.

A session the idle reaper (`reap.py`) closes is not the same as one you ended.
You did not decide it was finished; the host needed the memory, or nobody had
said anything for half a day. So it is marked **rested**, and the app can say
so ("resting — resume to pick up") rather than file it with the threads you
closed yourself. `/session/close` from the app never marks one.

The mark goes the moment the thread is used again: a reply or a routed ask
(`send.deliver`), a resume (`send.session_resume`), or the session turning up
live by any other road (the rows show `null` for a live session whatever the
file says, and the next reaper run drops the stale row).

Before resting a thread the reaper makes sure it has a recap newer than its
last message — Claude Code's own "while you were away" paragraph when there is
one, else one written here through the follow-up gateway call. Those are kept
too, so `recaps.recap_for` can fall back to them.

Files, under the state dir (each a `_jsonmap.JsonMap`):
  rested.json            {"<session>": {"at", "idle_h", "reason": "idle"|"idle-tight"}}
  generated-recaps.json  {"<session>": {"text", "at"}}
"""

from __future__ import annotations

import time

from ._jsonmap import JsonMap

RESTED = JsonMap("rested.json")
GENERATED = JsonMap("generated-recaps.json")

#: Why a session was rested: idle past the normal threshold, or past the
#: shorter one because the host was short of memory.
REASONS = ("idle", "idle-tight")


def mark_rested(session: str, idle_h: float, reason: str, at: float | None = None) -> None:
    if reason not in REASONS:
        raise ValueError(f"not a rest reason: {reason}")
    RESTED.put(session, {"at": round(at if at is not None else time.time(), 3),
                         "idle_h": round(float(idle_h), 2), "reason": reason})


def rested() -> dict[str, dict]:
    """`{session: {"at", "idle_h", "reason"}}` for every rested session."""
    return {k: v for k, v in RESTED.all().items() if isinstance(v, dict)}


def row_mark(session: str, live: bool, marks: dict | None = None) -> dict | None:
    """What a `/targets` row says: `{"at", "reason"}`, or None — always None
    for a live session, since a session that is running is not resting."""
    if live:
        return None
    m = (rested() if marks is None else marks).get(session)
    if not m:
        return None
    return {"at": m.get("at"), "reason": m.get("reason")}


def clear_rested(session: str) -> bool:
    return RESTED.drop(session)


def clear_quietly(session: str) -> None:
    """Drop the mark because the thread was used again. Never raises: the
    thing that used it already happened."""
    try:
        clear_rested(session)
    except Exception:  # noqa: BLE001
        pass


# --- the recaps written here -------------------------------------------------------


def save_recap(session: str, text: str, at: float | None = None) -> dict:
    row = {"text": text, "at": round(at if at is not None else time.time(), 3)}
    GENERATED.put(session, row)
    return row


def generated_recap(session: str) -> dict | None:
    """`{"text", "at"}` agent-media wrote for this session, or None."""
    row = GENERATED.get(session)
    if not isinstance(row, dict) or not row.get("text"):
        return None
    try:
        return {"text": str(row["text"]), "at": float(row.get("at") or 0)}
    except (TypeError, ValueError):
        return None
