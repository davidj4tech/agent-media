"""Changing notes from the app: a heading's TODO state, and moving it to
another GTD file — the two things an inbox needs besides capture.

  POST /notes/state  {"path", "at", "title", "state"}
      → {"path", "at", "state", "repeated", "next"?}
  POST /notes/refile {"path", "at", "title", "to", "date"?}
      → {"path", "at", "to"}
  POST /notes/date   {"path", "at", "title", "kind", "date", "time"?}
      → {"path", "at", "kind", "date", "time"}
  POST /notes/priority {"path", "at", "title", "priority": "A"|"B"|"C"|""}
      → {"path", "at", "priority"}

`at` is the heading's line as the app last saw it and `title` its text. The
file may have moved on since (a capture, an Emacs save, the sync), so the
heading is found by line and title together; if the line no longer holds
it, the one heading of that title is used, and two or none are a 409 —
the app refreshes and asks again.

Marking done does what Org does:
- a CLOSED timestamp on the planning line, taken off again when reopened;
- a heading with a repeater (`<2026-07-20 Mon +1d>`, `++1w`, `.+1m`) is not
  closed at all — its dates move to the next occurrence and it stays open.

Where a heading can be refiled to is the notes profile's (notes_profile.py):
each target is a file, the headline it goes under (or the top level), and
the state it takes. paragtd's follow its capture templates: next actions
under "* Inbox" in next-actions.org (as NEXT), the tickler under "* Tickler"
(SCHEDULED on the date given), and so on. The subtree moves whole, its
levels shifted to fit.

Changing a date rewrites the heading's SCHEDULED or DEADLINE stamp (`kind`)
in place, keeping its repeater and, unless `time` is given, its time of
day; `time: ""` drops the time. An empty `date` takes the stamp off, and
the planning line with it once nothing is left on it.

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
from .notes import _STARS, Keywords, _rel, _safe_path, keywords, profile, root


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

def _kw(lines: list[str]) -> Keywords:
    return keywords([ln for ln in lines if ln.startswith("#+")])


def _locate(lines: list[str], at: int, title: str) -> int:
    """The index of the heading the app means (see the module docstring)."""
    heading = _kw(lines).heading

    def title_at(i: int) -> str | None:
        m = heading.match(lines[i])
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
    m = _STARS.match(line)
    return len(m.group(1)) if m else 0


def _subtree_end(lines: list[str], i: int) -> int:
    level = _level(lines[i])
    for j in range(i + 1, len(lines)):
        if 0 < _level(lines[j]) <= level:
            return j
    return len(lines)


def _with_state(line: str, kw: Keywords, state: str, prio: str | None = None) -> str:
    """The heading line with `state` (and `prio`, when given; "" drops it)."""
    m = kw.heading.match(line)
    stars, _old, old_prio, title, tags = m.groups()
    prio = old_prio if prio is None else prio
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


def set_stamp(lines: list[str], i: int, kind: str, when: dt.date | None,
              time: str | None = None) -> str:
    """Put the heading's SCHEDULED or DEADLINE (`kind`) on `when`, in place:
    the old stamp's repeater stays, and its time of day unless `time` is
    given (`""` drops it). `when` None takes the stamp off, and the planning
    line with it once nothing is left on it. The stamp's time, now."""
    stamp_re = re.compile(rf"{kind}:\s*<([^>]*)>")
    plan = i + 1 if i + 1 < len(lines) and _PLANNING.match(lines[i + 1]) else None
    old = stamp_re.search(lines[plan]) if plan is not None else None
    new_time = ""
    if when:
        # What the old stamp carried after its date: a time, a repeater.
        parts = old.group(1).split()[1:] if old else []
        parts = [x for x in parts if not re.fullmatch(r"[^\s\d]+", x)]  # the weekday
        old_time = next((x for x in parts if _TIME.fullmatch(x)), "")
        rest = [x for x in parts if x != old_time]
        new_time = old_time if time is None else time
        inner = " ".join([when.isoformat(), _DAYS[when.weekday()]]
                         + ([new_time] if new_time else []) + rest)
        stamp = f"{kind}: <{inner}>"
        if old:
            lines[plan] = stamp_re.sub(lambda _m: stamp, lines[plan], count=1)
        elif plan is not None:
            lines[plan] = f"{lines[plan].rstrip()} {stamp}"
        else:
            lines.insert(i + 1, " " * (_level(lines[i]) + 1) + stamp)
    elif old:
        indent = re.match(r"\s*", lines[plan]).group(0)
        rest_line = re.sub(r"\s+", " ", stamp_re.sub("", lines[plan])).strip()
        if rest_line:
            lines[plan] = indent + rest_line
        else:
            del lines[plan]
    return new_time


# --- dependencies ------------------------------------------------------------------

_PROP = re.compile(r"^\s*:([A-Za-z0-9_@#%-]+):\s*(.*?)\s*$")


def properties(lines: list[str], i: int) -> dict[str, str]:
    """The heading's property drawer (after its planning line), keys upper-cased."""
    j = i + 1
    if j < len(lines) and _PLANNING.match(lines[j]):
        j += 1
    if j >= len(lines) or lines[j].strip().upper() != ":PROPERTIES:":
        return {}
    out: dict[str, str] = {}
    for k in range(j + 1, len(lines)):
        if lines[k].strip().upper() == ":END:" or _level(lines[k]):
            break
        m = _PROP.match(lines[k])
        if m:
            out[m.group(1).upper()] = m.group(2)
    return out


def parent(lines: list[str], i: int) -> int | None:
    level = _level(lines[i])
    return next((j for j in range(i - 1, -1, -1) if 0 < _level(lines[j]) < level), None)


def _is_open(lines: list[str], j: int, kw: Keywords) -> bool:
    m = kw.heading.match(lines[j])
    return bool(m and m.group(2) and m.group(2) not in kw.done)


def _title(lines: list[str], j: int, kw: Keywords) -> str:
    return kw.heading.match(lines[j]).group(4).strip()


def blocked_by(lines: list[str], i: int, kw: Keywords) -> str | None:
    """What stops the heading closing under `org-enforce-todo-dependencies`,
    as Org decides it (org-block-todo-from-children-or-siblings-or-parent):
    an open child; or, under an `:ORDERED:` parent, an open heading before
    it; and the same asked of each open ancestor in turn. The title of the
    one in the way, or None."""
    level = _level(lines[i])
    for j in range(i + 1, _subtree_end(lines, i)):
        if _level(lines[j]) == level + 1 and _is_open(lines, j, kw):
            return _title(lines, j, kw)
    here = i
    while (up := parent(lines, here)) is not None:
        if properties(lines, up).get("ORDERED", "").lower() not in ("", "nil"):
            for j in range(up + 1, here):
                if _level(lines[j]) and _is_open(lines, j, kw):
                    return _title(lines, j, kw)
        if not _is_open(lines, up, kw):
            return None
        here = up
    return None


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
    """Only the profile's files are edited from the app; roam notes are
    read-only."""
    p = _safe_path(rel)
    if not p or not profile().editable(root(), p.relative_to(root().resolve()).as_posix()):
        raise Refused("only the task files (inbox, next actions, …) can be changed here", 400)
    return p


# --- the routes' bodies ------------------------------------------------------------

def set_state(rel: str, at: int, title: str, state: str, bearer: str,
              now: dt.datetime | None = None) -> tuple[bool, dict]:
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    state = (state or "").strip().upper()
    now = now or dt.datetime.now()
    try:
        path = _gtd_path(rel)
        with ExitStack() as stack:
            f = _Locked(path, stack)
            kw = _kw(f.lines)
            if state and state not in kw.all:
                raise Refused(f"not a state in {_rel(path)}: {state!r}", 400)
            i = _locate(f.lines, at, title)
            old = kw.heading.match(f.lines[i]).group(2) or ""
            plan = i + 1 if i + 1 < len(f.lines) and _PLANNING.match(f.lines[i + 1]) else None
            closing = state in kw.done and old not in kw.done
            prof = profile()
            if closing and prof.enforces_dependencies():
                who = blocked_by(f.lines, i, kw)
                if who:
                    raise Refused(f"it waits on “{who}”, which is still open", 409)
            if closing and plan is not None and _REPEAT.search(f.lines[plan]):
                f.lines[plan], nxt = _advance(f.lines[plan], now.date())
                f.save()
                return True, {"path": _rel(path), "at": i + 1, "state": old,
                              "repeated": True, "next": nxt}
            f.lines[i] = _with_state(f.lines[i], kw, state)
            if closing:
                closed = f"CLOSED: {_stamp(now)}"
                if plan is None:
                    f.lines.insert(i + 1, " " * (_level(f.lines[i]) + 1) + closed)
                else:
                    indent = re.match(r"\s*", f.lines[plan]).group(0)
                    f.lines[plan] = f"{indent}{closed} {f.lines[plan].strip()}"
            elif state not in kw.done and old in kw.done and plan is not None:
                indent = re.match(r"\s*", f.lines[plan]).group(0)
                rest = _CLOSED.sub("", f.lines[plan]).strip()
                if rest:
                    f.lines[plan] = indent + rest
                else:
                    del f.lines[plan]
            extra = prof.after_state(f.lines, i, old, state, kw, now) or {}
            f.save()
            return True, {"path": _rel(path), "at": i + 1, "state": state, "repeated": False,
                          **extra}
    except Refused as e:
        return False, e.detail
    except OSError as e:
        return False, {"error": f"could not change the file ({e})", "status": 500}


def refile(rel: str, at: int, title: str, to: str, bearer: str,
           date: str = "") -> tuple[bool, dict]:
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    target = profile().refile_targets(root()).get((to or "").strip())
    if not target:
        return False, {"error": f"cannot move a heading to {to!r}", "status": 400}
    fname, headline, state, dated = target
    when = None
    if dated:
        try:
            when = dt.date.fromisoformat((date or "").strip())
        except ValueError:
            return False, {"error": f"moving it to the {to} needs a date (YYYY-MM-DD)",
                           "status": 400}
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
            src_kw = _kw(src.lines)
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
                tree[0] = _with_state(tree[0], src_kw, state)
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


_TIME = re.compile(r"\d{1,2}:\d{2}(?:-\d{1,2}:\d{2})?")


def set_date(rel: str, at: int, title: str, kind: str, date: str, bearer: str,
             time: str | None = None) -> tuple[bool, dict]:
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    kind = (kind or "").strip().upper()
    if kind not in ("SCHEDULED", "DEADLINE"):
        return False, {"error": f"not a date kind: {kind!r}", "status": 400}
    when = None
    if (date or "").strip():
        try:
            when = dt.date.fromisoformat(date.strip())
        except ValueError:
            return False, {"error": "the date must be YYYY-MM-DD", "status": 400}
    if time is not None:
        time = time.strip()
        if time and not _TIME.fullmatch(time):
            return False, {"error": "the time must be HH:MM", "status": 400}
    try:
        path = _gtd_path(rel)
        with ExitStack() as stack:
            f = _Locked(path, stack)
            i = _locate(f.lines, at, title)
            new_time = set_stamp(f.lines, i, kind, when, time)
            f.save()
            return True, {"path": _rel(path), "at": i + 1, "kind": kind.lower(),
                          "date": when.isoformat() if when else "", "time": new_time}
    except Refused as e:
        return False, e.detail
    except OSError as e:
        return False, {"error": f"could not change the file ({e})", "status": 500}


def set_priority(rel: str, at: int, title: str, priority: str, bearer: str) -> tuple[bool, dict]:
    """Set or clear the heading's `[#A]` cookie. An `[#A]` with a clock time
    on its SCHEDULED or DEADLINE is read aloud at that time (agent_media_core
    agenda_alarm.py)."""
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    priority = (priority or "").strip().upper()
    if priority not in ("", "A", "B", "C"):
        return False, {"error": f"not a priority: {priority!r}", "status": 400}
    try:
        path = _gtd_path(rel)
        with ExitStack() as stack:
            f = _Locked(path, stack)
            i = _locate(f.lines, at, title)
            kw = _kw(f.lines)
            state = kw.heading.match(f.lines[i]).group(2) or ""
            new = _with_state(f.lines[i], kw, state, priority)
            if new != f.lines[i]:
                f.lines[i] = new
                f.save()
            return True, {"path": _rel(path), "at": i + 1, "priority": priority}
    except Refused as e:
        return False, e.detail
    except OSError as e:
        return False, {"error": f"could not change the file ({e})", "status": 500}
