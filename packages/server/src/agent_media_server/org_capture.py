"""Org capture templates, filled and filed without Emacs.

A profile lists its templates (`Profile.capture_kinds`; paragtd's come from
the `.paragtd.json` its Emacs writes). Each is an Org capture `entry` template
with a file target, and this module does what `org-capture` would with the
parts a phone can supply:

  %?            what was typed: its first line there, the rest as the body
  %U %u %T %t   now, as an inactive/active stamp, with/without the time
  %<fmt>        now, formatted (format-time-string's codes are strftime's)
  %^{Prompt}    a text field; %^{Prompt|a|b} a choice, the first the default
  %^{Prompt}t   a date field (T with a time, u/U inactive)
  %^t %^T       a date field; %^u %^U inactive
  %i %a         nothing: there is no region or link on a phone

Anything else (`%(sexp)`, `%[file]`, tags prompts, …) needs Emacs, so a
template that uses it is not offered. The targets are `file` (the end, or
the top with `:prepend`), `file+headline` (under it, made if missing) and
`file+olp+datetree` (the day's heading in a year/month/day tree, or
year/ISO-week/day with `:tree-type week`, made in date order).

  POST /org/capture {"text", "kind": "<template key>", "fields": {id: value}}

docs/proposals/2026-09-24-notes-core-and-paragtd.md.
"""

from __future__ import annotations

import datetime as dt
import re
from contextlib import ExitStack
from pathlib import Path

_ESCAPE = re.compile(r"%(\^\{[^}]*\}[tTuU]?|\^[tTuUgGCL]|<[^>]*>|\(|\[|:[\w-]+|.)", re.S)
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_HEAD = re.compile(r"^(\*+)\s")


class CaptureError(Exception):
    def __init__(self, error: str, status: int = 400):
        super().__init__(error)
        self.detail = {"error": error, "status": status}


def _parse(template: str) -> tuple[list[dict], dict[str, str]] | None:
    """The template's prompts, in order and each once, and which escape is
    which prompt; None when it uses something only Emacs can fill."""
    out: list[dict] = []
    ids: dict[str, str] = {}
    for m in _ESCAPE.finditer(template):
        e = m.group(1)
        if e in ("?", "U", "u", "T", "t", "i", "a") or e.startswith("<"):
            continue
        if e in ids:
            continue
        f: dict = {"id": f"f{len(out)}"}
        if e.startswith("^{"):
            body, suffix = e[2:].rsplit("}", 1)
            label, *options = body.split("|")
            f["label"] = label.strip() or "Value"
            if suffix:
                f.update(type="datetime" if suffix in "TU" else "date", active=suffix in "tT")
            elif options:
                f.update(type="choice", options=[o.strip() for o in options if o.strip()])
            else:
                f["type"] = "text"
        elif e in ("^t", "^T", "^u", "^U"):
            timed = e in ("^T", "^U")
            f.update(label="Date and time" if timed else "Date",
                     type="datetime" if timed else "date", active=e in ("^t", "^T"))
        else:
            return None
        ids[e] = f["id"]
        out.append(f)
    return out, ids


def fields(template: str) -> list[dict] | None:
    """The prompts a template asks, as the app draws them — `{id, label,
    type: text|choice|date|datetime, options?, active?}` — or None when it
    uses something only Emacs can fill."""
    got = _parse(template)
    return got[0] if got else None


def needs_text(template: str) -> bool:
    return "%?" in template


def _stamp(d: dt.date, time: str, active: bool) -> str:
    inner = f"{d.isoformat()} {_DAYS[d.weekday()]}" + (f" {time}" if time else "")
    return f"<{inner}>" if active else f"[{inner}]"


def _date_value(raw: str, label: str) -> tuple[dt.date, str]:
    raw = (raw or "").strip()
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{1,2}:\d{2}))?", raw)
    if not m:
        raise CaptureError(f"{label}: a date is needed (YYYY-MM-DD)")
    try:
        return dt.date.fromisoformat(m.group(1)), m.group(2) or ""
    except ValueError as e:
        raise CaptureError(f"{label}: {e}") from e


def fill(template: str, text: str, values: dict, now: dt.datetime) -> str:
    """The template with everything filled: the entry as it goes in the file
    (its own heading at level 1, as templates are written)."""
    got = _parse(template)
    if got is None:
        raise CaptureError("that template needs Emacs to fill it")
    specs, ids = got
    by_id = {f["id"]: f for f in specs}
    first, _, rest = text.strip().partition("\n")

    def one(m: re.Match) -> str:
        e = m.group(1)
        if e == "?":
            return first.strip()
        if e in ("U", "u", "T", "t"):
            return _stamp(now.date(), now.strftime("%H:%M") if e in "UT" else "", e in "Tt")
        if e in ("i", "a"):
            return ""
        if e.startswith("<"):
            return now.strftime(e[1:-1])
        f = by_id[ids[e]]
        raw = str(values.get(f["id"], "") or "").strip()
        if f["type"] in ("date", "datetime"):
            d, time = _date_value(raw, f["label"])
            return _stamp(d, time if f["type"] == "datetime" else "", f.get("active", True))
        if f["type"] == "choice":
            return raw or f["options"][0]
        if not raw:
            raise CaptureError(f"{f['label']}: nothing given")
        return raw.replace("\n", " ")

    out = _ESCAPE.sub(one, template)
    if not out.endswith("\n"):
        out += "\n"
    # The rest of what was typed is the body. A line that would read as a
    # heading of its own is indented one space, so a capture is one entry.
    for ln in rest.strip("\n").splitlines():
        out += f" {ln}\n" if ln.startswith("*") else f"{ln}\n"
    return out


def _shift(block: str, by: int) -> list[str]:
    return [("*" * by + ln) if _HEAD.match(ln) else ln for ln in block.rstrip("\n").split("\n")]


def _level(line: str) -> int:
    m = _HEAD.match(line)
    return len(m.group(1)) if m else 0


def _end(lines: list[str], i: int) -> int:
    level = _level(lines[i])
    return next((j for j in range(i + 1, len(lines)) if 0 < _level(lines[j]) <= level), len(lines))


def _title(line: str) -> str:
    return _HEAD.sub("", line, count=1).strip()


def _ensure(lines: list[str], parent: int | None, level: int, title: str) -> int:
    """The heading `title` at `level` under `parent` (None: the top of the
    file), made where date order puts it if it is not there."""
    start = parent + 1 if parent is not None else 0
    stop = _end(lines, parent) if parent is not None else len(lines)
    later = None
    for j in range(start, stop):
        if _level(lines[j]) == level:
            if _title(lines[j]) == title:
                return j
            if later is None and _title(lines[j]) > title:
                later = j
    at = later if later is not None else stop
    if later is None:
        while at > start and not lines[at - 1].strip():
            at -= 1
    lines.insert(at, "*" * level + " " + title)
    return at


def _datetree(lines: list[str], day: dt.date, tree_type: str) -> int:
    if tree_type == "week":
        year, week, _ = day.isocalendar()
        y = _ensure(lines, None, 1, str(year))
        mid = _ensure(lines, y, 2, f"{year}-W{week:02d}")
    else:
        y = _ensure(lines, None, 1, str(day.year))
        mid = _ensure(lines, y, 2, f"{day:%Y-%m} {day:%B}")
    return _ensure(lines, mid, 3, f"{day.isoformat()} {_DAY_NAMES[day.weekday()]}")


def place(lines: list[str], tpl: dict, entry: str, now: dt.datetime) -> int:
    """Put `entry` in the file's `lines` where the template's target says.
    The index of its first line."""
    target = tpl.get("target") or "file"
    prepend = bool(tpl.get("prepend"))
    if target == "file":
        parent, level = None, 1
    elif target == "file+headline":
        head = tpl.get("headline") or ""
        parent = next((j for j, ln in enumerate(lines)
                       if _level(ln) and _title(ln) == head), None)
        if parent is None:
            while lines and not lines[-1].strip():
                lines.pop()
            if lines:
                lines.append("")
            lines.append(f"* {head}")
            parent = len(lines) - 1
        level = _level(lines[parent]) + 1
    elif target == "file+olp+datetree":
        parent = _datetree(lines, now.date(), tpl.get("tree_type") or "month")
        level = _level(lines[parent]) + 1
    else:
        raise CaptureError(f"a {target} target needs Emacs")
    block = _shift(entry, level - 1)
    if parent is None:
        if prepend:
            at = next((j for j, ln in enumerate(lines) if _level(ln)), len(lines))
        else:
            at = len(lines)
            while at > 0 and not lines[at - 1].strip():
                at -= 1
    elif prepend:
        at = parent + 1
        while at < len(lines) and not _level(lines[at]):
            at += 1
    else:
        at = _end(lines, parent)
        while at > parent + 1 and not lines[at - 1].strip():
            at -= 1
    lines[at:at] = block
    return at


def target_path(root: Path, rel: str) -> Path:
    """The template's file, which must be an `.org` file inside the tree."""
    rel = (rel or "").strip().lstrip("/")
    base = root.resolve()
    p = (base / rel).resolve()
    try:
        p.relative_to(base)
    except ValueError as e:
        raise CaptureError("that template files outside the notes") from e
    if p.suffix != ".org" or any(part.startswith(".") for part in Path(rel).parts):
        raise CaptureError("that template does not file into an .org file")
    return p


def capture(root: Path, tpl: dict, text: str, values: dict,
            now: dt.datetime | None = None) -> dict:
    """Fill `tpl` and file it. `{path, at}` — `at` the entry's line."""
    from .org_edit import _Locked

    now = now or dt.datetime.now()
    if needs_text(tpl["template"]) and not text.strip():
        raise CaptureError("nothing to capture")
    entry = fill(tpl["template"], text, values or {}, now)
    path = target_path(root, tpl["file"])
    with ExitStack() as stack:
        f = _Locked(path, stack)
        at = place(f.lines, tpl, entry, now)
        f.save()
    return {"path": path.relative_to(root.resolve()).as_posix(), "at": at + 1}
