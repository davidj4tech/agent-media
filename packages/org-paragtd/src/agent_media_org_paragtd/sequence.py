"""Sequenced projects, from the phone.

paragtd's `paragtd-sequence-subtree` (`C-c s`) marks a project `:ORDERED: t`
and puts a trigger on every step but the last:

    :TRIGGER: next-sibling todo!(NEXT) scheduled!("++2d")

In Emacs, org-edna runs it when the step is closed: the next step becomes
NEXT and is scheduled two days from that moment. Closing the step from the
Organiser does the same here. This is the one trigger form paragtd writes,
done again from its README's description, not org-edna itself; any other
trigger is left for Emacs, and the answer says so (`"trigger": "skipped"`).
The blocking half (`:ORDERED:`) is Org's own and lives in core
(`org_edit.blocked_by`).
"""

from __future__ import annotations

import datetime as dt
import re

from agent_media_server.org_edit import _level, _subtree_end, _with_state, properties, set_stamp

_TRIGGER = re.compile(
    r'^next-sibling\s+todo!\((?P<state>[^)\s]+)\)'
    r'(?:\s+scheduled!\("\+\+(?P<days>\d+)d"\))?\s*$')


def next_sibling(lines: list[str], i: int) -> int | None:
    """The next heading at this one's level under the same parent."""
    level = _level(lines[i])
    j = _subtree_end(lines, i)
    if j < len(lines) and _level(lines[j]) == level:
        return j
    return None


def after_state(lines: list[str], i: int, old: str, new: str, kw, now: dt.datetime) -> dict | None:
    if new not in kw.done or old in kw.done:
        return None
    trigger = properties(lines, i).get("TRIGGER", "")
    if not trigger:
        return None
    m = _TRIGGER.match(trigger)
    if not m:
        return {"trigger": "skipped"}
    j = next_sibling(lines, i)
    if j is None:
        return {"trigger": "no next step"}
    state = m.group("state")
    if state not in kw.all:
        return {"trigger": "skipped"}
    lines[j] = _with_state(lines[j], kw, state)
    out = {"title": kw.heading.match(lines[j]).group(4).strip(), "state": state}
    if m.group("days") is not None:
        when = now.date() + dt.timedelta(days=int(m.group("days")))
        set_stamp(lines, j, "SCHEDULED", when)
        out["scheduled"] = when.isoformat()
    out["at"] = j + 1
    return {"trigger": "ran", "triggered": out}
