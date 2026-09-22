"""Changing notes from the app: a heading's TODO state, and moving it to
another GTD file — the two things an inbox needs besides capture.

  POST /notes/state  {"path", "at", "title", "state"}
      → {"path", "at", "state", "repeated", "next"?}
  POST /notes/refile {"path", "at", "title", "to", "date"?}
      → {"path", "at", "to"}

`at` is the heading's line as the app last saw it and `title` its text. The
file may have moved on since (a capture, an Emacs save, the sync), so the
heading is found by line and title together; if the line no longer holds
it, the one heading of that title is used, and two or none are a 409 —
the app refreshes and asks again.

Marking done does what Org does:
- a CLOSED timestamp on the planning line, taken off again when reopened;
- a heading with a repeater (`<2026-07-20 Mon +1d>`, `++1w`, `.+1m`) is not
  closed at all — its dates move to the next occurrence and it stays open.

Refiling follows paragtd's capture templates: next actions under "* Inbox"
in next-actions.org (as NEXT), waiting-for under "* Waiting" (as WAITING),
the tickler under "* Tickler" (SCHEDULED on the date given), someday and
projects at the top level of their files. The subtree moves whole, its
levels shifted to fit.

Every file touched is flocked while it is read and rewritten, as capture
locks the inbox, so the two never interleave. Emacs sees a changed file.
"""

from __future__ import annotations

import calendar
import datetime as dt
import fcntl
import re
from contextlib import ExitStack
from pathlib import Path

from . import auth
from .notes import DONE_STATES, GTD_FILES, _HEADING, _rel, _safe_path, root

#: `to` → (file, the headline it goes under or None for top level, state or None).
REFILE_TARGETS = {
    "next": ("next-actions.org", "Inbox", "NEXT"),
    "waiting": ("waiting-for.org", "Waiting", "WAITING"),
    "tickler": ("tickler.org", "Tickler", None),
    "someday": ("someday.org", None, None),
    "projects": ("projects.org", None, None),
    "inbox": ("inbox.org", None, None),
}

SETTABLE = ("", "TODO", "NEXT", "WAITING", "SOMEDAY", "DONE", "CANCELLED")

_PLANNING = re.compile(r"^\s*(?:SCHEDULED|DEADLINE|CLOSED):")
_CLOSED = re.compile(r"\s*CLOSED:\s*\[[^\]]*\]")
_REPEAT = re.compile(
    r"<(\d{4}-\d{2}-\d{2})(?: [^\s>\d]+)?((?: \d{1,2}:\d{2}(?:-\d{1,2}:\d{2})?)?)"
    r"\s+(\.\+|\+\+|\+)(\d+)([dwmy])([^>]*)>")
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class Refused(Exception):
    def __init__(self, error: str, status: int):
        super().__init__(error)
        self.detail = {"error": error, "status": status}


# --- finding the heading ----------------------------------------------------------

def _locate(lines: list[str], at: int, title: str) -> int:
    """The index of the heading the app means (see the module docstring)."""
    def title_at(i: int) -> str | None:
        m = _HEADING.match(lines[i])
        return m.group(4).strip() if m else None

    title = (title or "").strip()
    if 0 < at <= len(lines) and title_at(at - 1) is not None:
        if not title or title_at(at - 1) == title:
            return at - 1
    if title:
        hits = [i for i in range(len(lines)) if title_at(i) == title]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise Refused("more than one heading has that title now; refresh and try again", 409)
    raise Refused("that heading is not there any more (the file changed?)", 409)


def _level(line: str) -> int:
    m = _HEADING.match(line)
    return len(m.group(1)) if m else 0


def _subtree_end(lines: list[str], i: int) -> int:
    level = _level(lines[i])
    for j in range(i + 1, len(lines)):
        if 0 < _level(lines[j]) <= level:
            return j
    return len(lines)


def _with_state(line: str, state: str) -> str:
    m = _HEADING.match(line)
    stars, _old, prio, title, tags = m.groups()
    parts = [stars] + ([state] if state else []) + ([f"[#{prio}]"] if prio else []) + [title.strip()]
    out = " ".join(parts)
    return f"{out} {tags}" if tags else out


def _stamp(now: dt.datetime) -> str:
    return f"[{now:%Y-%m-%d} {_DAYS[now.weekday()]} {now:%H:%M}]"


# --- repeaters ---------------------------------------------------------------------

def _add(d: dt.date, n: int, unit: str) -> dt.date:
    if unit == "d":
        return d + dt.timedelta(days=n)
    if unit == "w":
        return d + dt.timedelta(weeks=n)
    months = n * (12 if unit == "y" else 1)
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def _advance(line: str, today: dt.date) -> tuple[str, str | None]:
    """Move every repeating timestamp on a planning line to its next
    occurrence, as Org does on DONE. `(line, first new date or None)`."""
    first: list[str] = []

    def one(m: re.Match) -> str:
        date, time_, kind, n, unit, rest = m.groups()
        d, n = dt.date.fromisoformat(date), int(n)
        if kind == ".+":
            d = _add(today, n, unit)
        elif kind == "++":
            d = _add(d, n, unit)
            while d <= today:
                d = _add(d, n, unit)
        else:
            d = _add(d, n, unit)
        first.append(d.isoformat())
        return f"<{d.isoformat()} {_DAYS[d.weekday()]}{time_} {kind}{n}{unit}{rest}>"

    return _REPEAT.sub(one, line), (first[0] if first else None)


# --- files, locked -----------------------------------------------------------------

class _Locked:
    """A file opened for read-modify-write under an exclusive flock."""

    def __init__(self, path: Path, stack: ExitStack):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.f = stack.enter_context(path.open("a+"))
        fcntl.flock(self.f, fcntl.LOCK_EX)
        self.f.seek(0)
        self.lines = self.f.read().splitlines()

    def save(self) -> None:
        self.f.seek(0)
        self.f.truncate()
        self.f.write("\n".join(self.lines) + ("\n" if self.lines else ""))
        self.f.flush()


def _gtd_path(rel: str) -> Path:
    """Only the GTD files are edited from the app; roam notes are read-only."""
    p = _safe_path(rel)
    names = {name for _, _, name in GTD_FILES} | {"inbox.org"}
    if not p or p.parent != root().resolve() or p.name not in names:
        raise Refused("only the GTD files (inbox, next actions, …) can be changed here", 400)
    return p


# --- the routes' bodies ------------------------------------------------------------

def set_state(rel: str, at: int, title: str, state: str, bearer: str,
              now: dt.datetime | None = None) -> tuple[bool, dict]:
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    state = (state or "").strip().upper()
    if state not in SETTABLE:
        return False, {"error": f"not a state: {state!r}", "status": 400}
    now = now or dt.datetime.now()
    try:
        path = _gtd_path(rel)
        with ExitStack() as stack:
            f = _Locked(path, stack)
            i = _locate(f.lines, at, title)
            old = _HEADING.match(f.lines[i]).group(2) or ""
            plan = i + 1 if i + 1 < len(f.lines) and _PLANNING.match(f.lines[i + 1]) else None
            closing = state in DONE_STATES and old not in DONE_STATES
            if closing and plan is not None and _REPEAT.search(f.lines[plan]):
                f.lines[plan], nxt = _advance(f.lines[plan], now.date())
                f.save()
                return True, {"path": _rel(path), "at": i + 1, "state": old,
                              "repeated": True, "next": nxt}
            f.lines[i] = _with_state(f.lines[i], state)
            if closing:
                closed = f"CLOSED: {_stamp(now)}"
                if plan is None:
                    f.lines.insert(i + 1, " " * (_level(f.lines[i]) + 1) + closed)
                else:
                    indent = re.match(r"\s*", f.lines[plan]).group(0)
                    f.lines[plan] = f"{indent}{closed} {f.lines[plan].strip()}"
            elif state not in DONE_STATES and old in DONE_STATES and plan is not None:
                indent = re.match(r"\s*", f.lines[plan]).group(0)
                rest = _CLOSED.sub("", f.lines[plan]).strip()
                if rest:
                    f.lines[plan] = indent + rest
                else:
                    del f.lines[plan]
            f.save()
            return True, {"path": _rel(path), "at": i + 1, "state": state, "repeated": False}
    except Refused as e:
        return False, e.detail
    except OSError as e:
        return False, {"error": f"could not change the file ({e})", "status": 500}


def refile(rel: str, at: int, title: str, to: str, bearer: str,
           date: str = "") -> tuple[bool, dict]:
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    target = REFILE_TARGETS.get((to or "").strip())
    if not target:
        return False, {"error": f"cannot move a heading to {to!r}", "status": 400}
    fname, headline, state = target
    when = None
    if to == "tickler":
        try:
            when = dt.date.fromisoformat((date or "").strip())
        except ValueError:
            return False, {"error": "the tickler needs a date (YYYY-MM-DD)", "status": 400}
    try:
        src_path = _gtd_path(rel)
        dst_path = root() / fname
        if dst_path.resolve() == src_path:
            return False, {"error": f"it is already in {fname}", "status": 400}
        with ExitStack() as stack:
            # Always lock in the same order, so two moves cannot deadlock.
            first, second = sorted([src_path, dst_path.resolve()], key=str)
            locked = {p: _Locked(p, stack) for p in (first, second)}
            src, dst = locked[src_path], locked[dst_path.resolve()]
            i = _locate(src.lines, at, title)
            end = _subtree_end(src.lines, i)
            tree = src.lines[i:end]
            del src.lines[i:end]

            # Where it lands, and the level it lands at.
            if headline:
                h = next((j for j, ln in enumerate(dst.lines)
                          if re.fullmatch(rf"\*\s+{re.escape(headline)}\s*", ln)), None)
                if h is None:
                    if dst.lines and dst.lines[-1].strip():
                        dst.lines.append("")
                    dst.lines.append(f"* {headline}")
                    h = len(dst.lines) - 1
                insert, level = _subtree_end(dst.lines, h), 2
                # Before the blank lines that end the headline's subtree.
                while insert > h + 1 and not dst.lines[insert - 1].strip():
                    insert -= 1
            else:
                while dst.lines and not dst.lines[-1].strip():
                    dst.lines.pop()
                insert, level = len(dst.lines), 1

            shift = level - _level(tree[0])
            tree = [("*" * (_level(ln) + shift) + ln[_level(ln):]) if _level(ln) else ln
                    for ln in tree]
            if state:
                tree[0] = _with_state(tree[0], state)
            if when:
                stamp = f"SCHEDULED: <{when.isoformat()} {_DAYS[when.weekday()]}>"
                if len(tree) > 1 and _PLANNING.match(tree[1]):
                    line = re.sub(r"SCHEDULED:\s*<[^>]*>", "", tree[1]).rstrip()
                    indent = re.match(r"\s*", tree[1]).group(0)
                    tree[1] = f"{indent}{stamp} {line.strip()}".rstrip()
                else:
                    tree.insert(1, " " * (level + 1) + stamp)
            dst.lines[insert:insert] = tree
            dst.save()
            src.save()
            return True, {"path": fname, "at": insert + 1, "to": to}
    except Refused as e:
        return False, e.detail
    except OSError as e:
        return False, {"error": f"could not move it ({e})", "status": 500}
