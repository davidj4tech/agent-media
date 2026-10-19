"""The Organiser's calendar: what the agenda files hold for any range of days.

  GET /calendar?from=YYYY-MM-DD&to=YYYY-MM-DD[&done=1]
      → {"from", "to", "today", "items": [...]}

The agenda (org.py `_agenda`) is a list of the coming week and keeps only a
heading's date. A calendar needs more of each timestamp: its time and end
time (`<2026-10-21 Wed 10:00-11:30>`), a span (`<…>--<…>`), and its repeater
(`+1w`, `++1m`, `.+2d`), expanded into every day it falls on inside the
range, as Org's own week view shows them.

Each item is one heading on one day: the heading's fields as the agenda has
them (path, at, level, state, priority, title, tags), and
`type` (scheduled, deadline or event: a plain active timestamp in the
heading or its body), `date`, then where the stamp has them `time`,
`end_time`, `end_date` (a span's last day) and `repeat`. A scheduled or
deadline heading not yet done whose date has passed is also listed on today,
when today is in the range and no repeat of it falls there, with
`overdue: true` and `due` its own date, as Org carries it forward. Events are never overdue.

Later sources (calendar services, the phone's calendar) join the same list;
docs/proposals/2026-10-20-organiser-calendar.md.
"""

from __future__ import annotations

import calendar as _cal
import datetime as dt
import re
from pathlib import Path

from . import auth, org

#: The widest range one request may ask for: a month view with its edges.
MAX_SPAN_DAYS = 92

MAX_ITEMS = 2000

_STAMP = re.compile(
    r"<(\d{4}-\d{2}-\d{2})"              # the date
    r"(?:\s+[^\s\d>]+)?"                 # its day name, in any language
    r"(?:\s+(\d{1,2}:\d{2})(?:-(\d{1,2}:\d{2}))?)?"  # a time, an end time
    r"([^>]*)>"                          # repeater and warning
    r"(?:--<(\d{4}-\d{2}-\d{2})(?:\s+[^\s\d>]+)?(?:\s+(\d{1,2}:\d{2}))?[^>]*>)?")
_PLANNED = re.compile(r"\b(SCHEDULED|DEADLINE):\s*(<[^>]*>)")
_REPEAT = re.compile(r"(?<![-\w])(\.\+|\+\+|\+)(\d+)([dwmy])\b")


def _stamp(text: str) -> dict | None:
    """One active timestamp's parts, or None when it is not one."""
    m = _STAMP.search(text)
    if not m:
        return None
    date, time, end_time, rest, end_date, span_end_time = m.groups()
    try:
        dt.date.fromisoformat(date)
        if end_date:
            dt.date.fromisoformat(end_date)
    except ValueError:
        return None
    out: dict = {"date": date}
    if time:
        out["time"] = time.zfill(5)
    if end_time:
        out["end_time"] = end_time.zfill(5)
    if end_date and end_date > date:
        out["end_date"] = end_date
        if span_end_time:
            out["end_time"] = span_end_time.zfill(5)
    if r := _REPEAT.search(rest or ""):
        out["repeat"] = r.group(0)
        out["_every"] = (int(r.group(2)), r.group(3))
    return out


def _entries(path: Path) -> list[tuple[dict, list[dict]]]:
    """Each heading of one file with its stamps: SCHEDULED, DEADLINE, and
    the first plain active timestamp in its title or body (an event)."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    kw = org.keywords([ln for ln in lines if ln.startswith("#+")])
    rel = path.relative_to(org.root()).as_posix()
    out: list[tuple[dict, list[dict]]] = []
    event = False
    for i, line in enumerate(lines):
        m = kw.heading.match(line)
        if m:
            stars, state, prio, title, tags = m.groups()
            title = title.strip()
            stamps: list[dict] = []
            if (s := _stamp(title)):
                stamps.append({**s, "type": "event"})
                title = _STAMP.sub("", title).strip() or title
            event = bool(stamps)
            out.append(({"path": rel, "at": i + 1, "level": len(stars),
                         "state": state or "", "priority": prio or "",
                         "title": title, "done": (state or "") in kw.done,
                         "tags": [t for t in (tags or "").split(":") if t]}, stamps))
            continue
        if not out:
            continue
        stripped = line.lstrip()
        if stripped.startswith(("SCHEDULED", "DEADLINE")):
            for kind, raw in _PLANNED.findall(line):
                if (s := _stamp(raw)):
                    out[-1][1].append({**s, "type": kind.lower()})
        elif not event and not stripped.startswith(("CLOSED", ":")):
            if (s := _stamp(line)):
                out[-1][1].append({**s, "type": "event"})
                event = True
    return out


def _add(day: dt.date, n: int, unit: str) -> dt.date:
    if unit == "d":
        return day + dt.timedelta(days=n)
    if unit == "w":
        return day + dt.timedelta(weeks=n)
    months = n if unit == "m" else 12 * n
    y, mo = divmod(day.month - 1 + months, 12)
    y += day.year
    mo += 1
    return day.replace(year=y, month=mo, day=min(day.day, _cal.monthrange(y, mo)[1]))


def _days(stamp: dict, start: dt.date, end: dt.date) -> list[dt.date]:
    """The days a stamp falls on between start and end, its repeats
    included. A span counts where any of its days is in the range."""
    base = dt.date.fromisoformat(stamp["date"])
    length = (dt.date.fromisoformat(stamp["end_date"]) - base).days if "end_date" in stamp else 0
    every = stamp.get("_every")
    if not every:
        return [base] if base <= end and base + dt.timedelta(days=length) >= start else []
    n, unit = every
    if n <= 0:
        return [base] if start <= base <= end else []
    # Each repeat is counted from the base, so 31 Jan +1m is 28 Feb, then
    # 31 Mar. Days and weeks skip straight to the range.
    k = 0
    if unit in "dw":
        step = n * (7 if unit == "w" else 1)
        k = max(0, ((start - base).days - length) // step)
    out = []
    for k in range(k, k + 5000):
        day = _add(base, n * k, unit)
        if day > end:
            break
        if day + dt.timedelta(days=length) >= start:
            out.append(day)
    return out


def items(start: dt.date, end: dt.date, *, today: dt.date | None = None,
          done: bool = False) -> list[dict]:
    today = today or dt.date.today()
    prof = org.profile()
    root = org.root()
    out: list[dict] = []
    for name in prof.agenda_files(root):
        for head, stamps in _entries(root / name):
            if head["done"] and not done:
                continue
            for stamp in stamps:
                base = dt.date.fromisoformat(stamp["date"])
                if base < today and not prof.agenda_keep(root, name, (today - base).days):
                    continue
                shown = {k: v for k, v in stamp.items() if not k.startswith("_")}
                row = {k: v for k, v in head.items() if k != "done"}
                days = _days(stamp, start, end)
                for day in days:
                    out.append({**row, **shown, "date": day.isoformat(),
                                "overdue": False})
                # A repeat that falls on today stands for it there.
                if (stamp["type"] != "event" and not head["done"]
                        and base < today and start <= today <= end
                        and today not in days):
                    out.append({**row, **{k: v for k, v in shown.items()
                                          if k not in ("time", "end_time", "end_date")},
                                "date": today.isoformat(), "due": stamp["date"],
                                "overdue": True})
    out.sort(key=lambda h: (h["date"], h.get("time") or "", h["path"], h["at"]))
    return out[:MAX_ITEMS]


def _day(text: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def get(qs_from: str, qs_to: str, bearer: str, *, done: bool = False) -> tuple[bool, dict]:
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    today = dt.date.today()
    start = _day(qs_from) if qs_from else today
    end = _day(qs_to) if qs_to else (start + dt.timedelta(days=6) if start else None)
    if not start or not end:
        return False, {"error": "from and to are dates (YYYY-MM-DD)", "status": 400}
    if end < start or (end - start).days >= MAX_SPAN_DAYS:
        return False, {"error": f"to is on or after from, at most {MAX_SPAN_DAYS} days on",
                       "status": 400}
    return True, {"from": start.isoformat(), "to": end.isoformat(),
                  "today": today.isoformat(),
                  "items": items(start, end, today=today, done=done)}
