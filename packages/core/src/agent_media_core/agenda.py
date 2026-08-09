"""The agenda, spoken.

This is the case where audio actually wins: short, time-bound, and wanted
while your hands are busy. It is also the case where reading the source
verbatim fails hardest. This agenda has 322 entries, 153 of them past their
scheduled date and 234 of them astrological transits — read straight out,
that is twenty minutes of noise with the one thing you needed buried in it.

So the shape is a briefing, not a dump: what today holds, then the backlog as
a *count* with a few examples, then what's coming. Every list is capped, and
every cap is spoken — "and 148 more" — because a silent truncation is heard as
"that's all of it", which is worse than the noise it was meant to spare you.

Where the entries come from is deliberately not core's business. `MEDIA_AGENDA_CMD`
names a command that prints a JSON array of entries, and a front end that knows
the user's real configuration — which files are agenda files, which keywords
mean done — can satisfy that contract. Core must keep working with that front
end uninstalled, dead or wedged, so it never reaches for one by name; when no
command is configured or it fails, there is a direct file scan, which is honest
about being an approximation.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .docs import Section, speak_inline_org

_TS = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_REPEATER = re.compile(r"[.+]{1,2}\d+[hdwmy]")


@dataclass
class Item:
    todo: str
    heading: str
    done: bool = False
    priority: str = ""
    tags: tuple = ()
    scheduled: Optional[_dt.date] = None
    deadline: Optional[_dt.date] = None
    repeating: bool = False
    file: str = ""

    @property
    def when(self) -> Optional[_dt.date]:
        """Deadlines outrank scheduling: one is a commitment, the other a plan."""
        return self.deadline or self.scheduled


def _date(raw: Optional[str]) -> Optional[_dt.date]:
    if not raw:
        return None
    m = _TS.search(raw)
    if not m:
        return None
    try:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _item(d: dict) -> Item:
    sched = d.get("scheduled") or ""
    return Item(
        todo=str(d.get("todo") or ""),
        heading=str(d.get("heading") or ""),
        done=bool(d.get("done")),
        priority=str(d.get("priority") or ""),
        tags=tuple(d.get("tags") or ()),
        scheduled=_date(sched),
        deadline=_date(d.get("deadline")),
        repeating=bool(_REPEATER.search(sched or "")
                       or _REPEATER.search(d.get("deadline") or "")),
        file=str(d.get("file") or ""),
    )


# --- sources ---------------------------------------------------------------

def entries_via_command(timeout: float = 60.0) -> Optional[list]:
    """Run `MEDIA_AGENDA_CMD` and parse its JSON. None if unset or it fails.

    The contract is one JSON array of objects with `todo`, `heading`, `done`,
    `tags`, `scheduled`, `deadline`, `file` — everything core needs and nothing
    about how the provider found it. A provider that knows the user's real
    configuration gives a better answer than any parser here could; one that is
    not installed or not running costs a fallback and nothing else.
    """
    cmd = os.environ.get("MEDIA_AGENDA_CMD", "").strip()
    if not cmd:
        return None
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        data = json.loads(r.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return [_item(d) for d in data] if isinstance(data, list) else None


_HEAD = re.compile(r"^\*+\s+([A-Z]{3,})\s+(.*?)(?:\s+(:[\w@#%:]+:))?\s*$")
_SCHED = re.compile(r"SCHEDULED:\s*([<\[][^>\]]+[>\]])")
_DEAD = re.compile(r"DEADLINE:\s*([<\[][^>\]]+[>\]])")
_DONE_WORDS = {"DONE", "CANCELLED", "CANCELED"}


def entries_via_files(paths: list) -> list:
    """A direct scan, for when no provider is configured or it failed.

    Approximate by construction: it cannot know a user's TODO keywords, so it
    treats an all-caps first word as a state and a known few as done. That is
    the price of not depending on the front end, and it is the right price —
    an approximate agenda beats none when Emacs is down.
    """
    items: list = []
    for p in paths:
        try:
            text = Path(p).read_text(errors="replace")
        except OSError:
            continue
        name = Path(p).name
        lines = text.splitlines()
        for i, line in enumerate(lines):
            m = _HEAD.match(line)
            if not m:
                continue
            state, heading = m.group(1), m.group(2)
            tail = "\n".join(lines[i + 1:i + 3])
            s = _SCHED.search(tail)
            d = _DEAD.search(tail)
            items.append(_item({
                "todo": state, "heading": heading,
                "done": state in _DONE_WORDS,
                "tags": [t for t in (m.group(3) or "").split(":") if t],
                "scheduled": s.group(1) if s else None,
                "deadline": d.group(1) if d else None,
                "file": name,
            }))
    return items


def agenda_files() -> list:
    raw = os.environ.get("MEDIA_AGENDA_FILES", "")
    if raw:
        return [Path(x).expanduser() for x in raw.split(":") if x.strip()]
    org = Path.home() / "org"
    return sorted(org.glob("*.org")) if org.is_dir() else []


def load_entries() -> list:
    got = entries_via_command()
    if got is not None:
        return got
    return entries_via_files(agenda_files())


# --- phrasing --------------------------------------------------------------

def say_date(day: _dt.date, today: _dt.date) -> str:
    """A date a person would say. Nobody hears "2026-08-11" as Tuesday."""
    delta = (day - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if 2 <= delta <= 6:
        return day.strftime("%A")
    if -6 <= delta <= -2:
        return f"{-delta} days ago"
    if delta < 0:
        weeks = round(-delta / 7)
        if weeks == 1:
            return "about a week ago"
        if weeks <= 8:
            return f"about {weeks} weeks ago"
        return day.strftime("%-d %B")
    return day.strftime("%-d %B")


def _plural(n: int, one: str, many: Optional[str] = None) -> str:
    return f"{n} {one}" if n == 1 else f"{n} {many or one + 's'}"


def say_item(it: Item, today: _dt.date, with_date: bool = True) -> str:
    bits = speak_inline_org(it.heading).rstrip(".")
    if it.priority:
        bits = f"priority {it.priority}, {bits}"
    if with_date and it.when:
        verb = "due" if it.deadline else "scheduled"
        bits += f", {verb} {say_date(it.when, today)}"
    return bits + "."


# --- the briefing ----------------------------------------------------------

CAP = 6


def _cap(items: list, today: _dt.date, cap: int = CAP,
         with_date: bool = True) -> list:
    """Read `cap` of them, then say how many were not read.

    The count is not decoration. A list that stops without saying so is heard
    as the whole list, and a backlog of 153 silently presented as 6 is a worse
    lie than reading all 153.
    """
    out = [say_item(i, today, with_date) for i in items[:cap]]
    rest = len(items) - cap
    if rest > 0:
        out.append(f"And {rest} more.")
    return out


def aside_files() -> set:
    """Files whose entries get their own chapter instead of the main lists.

    `astro.org` holds 234 of this agenda's 322 entries — generated transits,
    one per lunar ingress. They are wanted (they were put in the agenda on
    purpose) but they are not commitments, and mixed in they crowd the real
    ones out of every capped list: four of the six most recent overdue items
    were moon phases. Their own chapter keeps them, and lets a listener skip
    them in one keypress, which is what chapters are for.
    """
    raw = os.environ.get("MEDIA_AGENDA_ASIDE_FILES", "astro.org")
    return {x.strip() for x in raw.split(":") if x.strip()}


def _aside_title(files: set) -> str:
    if files == {"astro.org"}:
        return "Astrology"
    return "Also"


def agenda_sections(items: list, today: Optional[_dt.date] = None) -> list:
    today = today or _dt.date.today()
    aside_names = aside_files()
    open_all = [i for i in items if not i.done]
    aside = [i for i in open_all if i.file in aside_names]
    open_items = [i for i in open_all if i.file not in aside_names]

    overdue, due_today, soon, undated = [], [], [], []
    for i in open_items:
        w = i.when
        if w is None:
            undated.append(i)
        elif w < today:
            overdue.append(i)
        elif w == today:
            due_today.append(i)
        elif w <= today + _dt.timedelta(days=7):
            soon.append(i)
    overdue.sort(key=lambda i: i.when, reverse=True)     # most recent first
    soon.sort(key=lambda i: i.when)

    sections: list = []

    head = [f"{today.strftime('%A %-d %B')}."]
    if due_today:
        head.append(f"{_plural(len(due_today), 'thing')} today.")
    else:
        head.append("Nothing scheduled for today.")
    if overdue:
        n = len(overdue)
        head.append(f"{_plural(n, 'item')} past {'its' if n == 1 else 'their'} date.")
    if soon:
        head.append(f"{_plural(len(soon), 'thing')} in the next week.")
    sections.append(Section(heading="Agenda", text=" ".join(head)))

    if due_today:
        sections.append(Section(heading="Today",
                                text="\n".join(_cap(due_today, today, cap=10,
                                                    with_date=False))))
    if soon:
        sections.append(Section(heading="Coming up",
                                text="\n".join(_cap(soon, today, cap=8))))
    if overdue:
        # Newest first: the thing you missed yesterday is actionable, the one
        # from March is a decision about whether it was ever real.
        sections.append(Section(heading="Past their date",
                                text="\n".join(_cap(overdue, today))))
    if undated:
        sections.append(Section(heading="Undated",
                                text="\n".join(_cap(undated, today,
                                                    with_date=False))))
    if aside:
        near = [i for i in aside
                if i.when and today <= i.when <= today + _dt.timedelta(days=7)]
        near.sort(key=lambda i: i.when)
        body = _cap(near, today, cap=8) if near else ["Nothing this week."]
        body.append(f"{_plural(len(aside), 'entry', 'entries')} in total.")
        sections.append(Section(heading=_aside_title(aside_names),
                                text="\n".join(body)))
    return sections


def agenda_text(items: list, today: Optional[_dt.date] = None) -> str:
    parts = []
    for s in agenda_sections(items, today):
        if s.heading:
            parts.append(s.heading + ".")
        if s.text:
            parts.append(s.text)
    return "\n\n".join(parts)
