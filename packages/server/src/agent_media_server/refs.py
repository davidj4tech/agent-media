"""Another conversation, named in a message by a chip instead of by typing it.

The app writes `@[<title>]` into the box (Share… from a thread's menu, or
`@` in the composer) and sends what it knows as `refs`: `{title: session}`.
Before the words go anywhere, each chip gets a line at the foot saying which
session it is and where its transcript lives, so the agent can read it
rather than guess from the title:

    what did we decide in @[Gimbal notes]?

    @[Gimbal notes] is conversation 6c73… (claude, transcript /home/…/6c73….jsonl)

The chip stays in the text as written. A chip the app sent no session for
(a draft that came back from another device, a hand-typed one) is looked up
by title among the threads, and used only when exactly one has it. One that
resolves to nothing is left alone, with no line.

A Matrix room (matrix.py) has no transcript on the disk, so its line says
how to page back (`media room-log`) and its last messages follow it,
indented, oldest first.
"""
from __future__ import annotations

import re

from agent_media_core import harnesses

from . import sessions, transcript

ROOM_TAIL = 40          # a room's messages carried under its chip

CHIP = re.compile(r"@\[([^\[\]\n]{1,200})\]")


def chips(text: str) -> list[str]:
    """The titles chipped in `text`, first appearance order, once each."""
    seen: list[str] = []
    for m in CHIP.finditer(text or ""):
        label = m.group(1).strip()
        if label and label not in seen:
            seen.append(label)
    return seen


def _by_title(labels: list[str]) -> dict[str, str]:
    """Each label's session, where exactly one thread carries that title."""
    found: dict[str, list[str]] = {label.casefold(): [] for label in labels}
    try:
        rows = sessions.sessions_index()
    except Exception:
        return {}
    for row in rows:
        key = " ".join(str(row.get("title") or "").split()).casefold()
        if key in found and row.get("session") not in found[key]:
            found[key].append(row["session"])
    return {label: found[label.casefold()][0] for label in labels
            if len(found[label.casefold()]) == 1}


def _room_tail(r, limit: int = ROOM_TAIL) -> str:
    """`r`'s last `limit` messages as indented lines, oldest first, with how
    to read further back."""
    import datetime as dt

    msgs, older = r.page(limit)
    out = [f"(Matrix room {r.title()}; last {len(msgs)} messages below"
           + (f", older ones: media room-log {r.thread} --before {msgs[0]['id']})"
              if older and msgs else ")")]
    for m in msgs:
        text = " ".join(p.get("text") or "" for p in m.get("parts") or []).strip()
        when = dt.datetime.fromtimestamp(m.get("at") or 0).strftime("%d %b %H:%M")
        who = (m.get("sender") or {}).get("name") or m.get("role")
        out.append(f"    [{when}] {who}: {' '.join(text.split())[:2000]}")
    return "\n".join(out)


def line_for(label: str, session: str) -> str:
    from . import matrix

    r = matrix.room(session)
    if r is not None:
        return f"@[{label}] is conversation {session} {_room_tail(r)}"
    harness, path = transcript.transcript_of(session)
    if path:
        return f"@[{label}] is conversation {session} ({harness}, transcript {path})"
    return f"@[{label}] is conversation {session} (no transcript file here)"


def expand(text: str, refs=None) -> str:
    """`text` with a line at its foot for every chip that names a session."""
    labels = chips(text)
    if not labels:
        return text
    given: dict[str, str] = {}
    if isinstance(refs, dict):
        for label, session in refs.items():
            label, session = " ".join(str(label).split()), str(session or "").strip()
            if harnesses.SESSION_ID.fullmatch(session):
                given[label] = session
    missing = [label for label in labels if label not in given]
    if missing:
        given.update(_by_title(missing))
    lines = [line_for(label, given[label]) for label in labels if label in given]
    if not lines:
        return text
    return text.rstrip() + "\n\n" + "\n".join(lines)
