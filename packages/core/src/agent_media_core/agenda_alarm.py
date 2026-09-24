"""Priority-A TODOs read aloud at their time.

An Org TODO marked `[#A]` whose SCHEDULED or DEADLINE carries a clock time
(`<2026-09-24 Thu 14:00>`) is spoken when that time comes: "Priority A, now:
call the bank". Date-only ones stay quiet — the morning agenda has them.

`media agenda-alarm` does one pass; a systemd user timer runs it every minute
(deploy/systemd/agenda-alarm.*). A stamp is spoken once, and only within
WINDOW of its time, so a host that was asleep does not read out the
afternoon's alarms at once when it wakes. What has been said is kept in
`<state_dir>/agenda-alarms.json`, `{"<key>": <said at>}`, pruned after two
days.

The speech is alert-class (`media say --alert`): nobody asked for it at that
moment, so a phone on silent withholds it like any other timer's.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from ._paths import state_dir
from .agenda import Item, load_entries
from .docs import speak_inline_org

#: How late a stamp may still be spoken. A minute's timer plus slack.
WINDOW = _dt.timedelta(minutes=int(os.environ.get("MEDIA_AGENDA_ALARM_WINDOW_MIN", "10")))
_KEEP_S = 2 * 86400


@dataclass
class Alarm:
    key: str
    at: _dt.datetime
    kind: str          # "scheduled" | "deadline"
    item: Item


def _stamps(it: Item):
    if it.scheduled and it.scheduled_at:
        yield "scheduled", _dt.datetime.combine(it.scheduled, it.scheduled_at)
    if it.deadline and it.deadline_at:
        yield "deadline", _dt.datetime.combine(it.deadline, it.deadline_at)


def due(items: list, now: _dt.datetime) -> list:
    """Open priority-A stamps whose time is in (now - WINDOW, now]."""
    out = []
    for it in items:
        if it.done or it.priority != "A":
            continue
        for kind, at in _stamps(it):
            if at <= now < at + WINDOW:
                key = f"{it.file}|{it.heading}|{kind}|{at:%Y-%m-%d %H:%M}"
                out.append(Alarm(key, at, kind, it))
    return sorted(out, key=lambda a: a.at)


def phrase(a: Alarm) -> str:
    what = speak_inline_org(a.item.heading).strip().rstrip(".")
    if a.kind == "deadline":
        return f"Priority A, due now: {what}."
    return f"Priority A, now: {what}."


def _path() -> Path:
    return state_dir() / "agenda-alarms.json"


def _said() -> dict:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(said: dict) -> None:
    cutoff = time.time() - _KEEP_S
    said = {k: v for k, v in said.items() if isinstance(v, (int, float)) and v >= cutoff}
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(said, indent=0, sort_keys=True))
    tmp.replace(p)


def run(now: _dt.datetime | None = None, speak=None, dry_run: bool = False,
        items: list | None = None) -> list:
    """One pass: speak what is due and not yet said. Returns the phrases."""
    now = now or _dt.datetime.now()
    said = _said()
    fresh = [a for a in due(items if items is not None else load_entries(), now)
             if a.key not in said]
    spoken = []
    for a in fresh:
        text = phrase(a)
        spoken.append(text)
        if dry_run:
            continue
        if speak is None or speak(text):
            said[a.key] = time.time()
    if fresh and not dry_run:
        _save(said)
    return spoken
