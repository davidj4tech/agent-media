"""PHS payroll roster emails (in notmuch) -> David's current assigned shifts.

Employment Hero mails three kinds of roster notice: shifts assigned, a shift
changed (old -> new) and shifts removed. The mail carries no shift ids, so a
shift's id is its first-seen date and times; a change keeps the id and moves
the times. Replaying every notice oldest-first gives the current set.

Mail can be missed, and Gmail archiving a notice expunges it here (mbsync
syncs INBOX only), so every notice parsed once is kept in a small state file
of Message-IDs and times only (no names, addresses or bodies) and replayed with what notmuch
still holds. A notice that cannot be read is skipped and counted, never
raised, so one odd email does not stop the sync.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Australia/Melbourne")
QUERY = "from:payroll@peninsulahotsprings.com"
# Skipped notices older than this cannot plausibly hide a shift in the window.
RECENT = dt.timedelta(days=45)

_KINDS = (("removed", re.compile(r"no longer rostered", re.I)),
          ("changed", re.compile(r"\bhas been changed\b", re.I)),
          ("assigned", re.compile(r"assigned the following shifts", re.I)))
_SUBJECT = re.compile(r"new shifts are available|shift assigned to you has been changed"
                      r"|no longer rostered", re.I)
_TIME = re.compile(r"(\d{1,2}):(\d{2})\s*([ap])\.?m\.?\s*[-–—]\s*(\d{1,2}):(\d{2})\s*([ap])\.?m\.?", re.I)
_DATE = re.compile(r"([A-Za-z]{3})[a-z]*\.?,?\s*(\d{1,2})/(\d{1,2})/(\d{4})")


def text_of(body: str) -> list[str]:
    """HTML (also what the text/plain part holds) -> non-empty text lines."""
    body = re.sub(r"(?is)<(style|head|script)\b.*?</\1\s*>", "", body)
    body = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|td|th|li|h\d|table)\s*>", "\n", body)
    body = html.unescape(re.sub(r"<[^>]+>", " ", body))
    return [" ".join(line.split()) for line in body.splitlines() if line.strip()]


def _blocks(lines: list[str]) -> list[list[str]]:
    """Each shift as [start, end] ISO local stamps, in mail order."""
    out, day = [], None
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        label, _, rest = line.partition(":")
        label = label.strip().lower()
        if label == "day/date":
            m = _DATE.search(rest) or _DATE.search(nxt)
            if not m:
                raise ValueError("date")
            day = dt.date(int(m[4]), int(m[3]), int(m[2]))
            if day.strftime("%a").lower() != m[1][:3].lower():
                raise ValueError("weekday")  # guards a dd/mm vs mm/dd swap
        elif label == "time":
            m = _TIME.search(rest) or _TIME.search(nxt)
            if not m or day is None:
                raise ValueError("time")
            def clock(h, mins, half):
                h = int(h) % 12 + (12 if half.lower() == "p" else 0)
                return dt.time(h, int(mins))
            start = dt.datetime.combine(day, clock(m[1], m[2], m[3]))
            end = dt.datetime.combine(day, clock(m[4], m[5], m[6]))
            if end <= start:
                end += dt.timedelta(days=1)
            out.append([start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")])
            day = None
    if day is not None:
        raise ValueError("date without time")
    return out


def parse(lines: list[str]) -> dict | None:
    """One notice -> {kind, old, new}; None when it is not a roster notice.
    Raises ValueError for a roster notice whose shifts cannot be read."""
    text = "\n".join(lines)
    kind = next((k for k, pattern in _KINDS if pattern.search(text)), None)
    if kind is None:
        return None
    if kind == "changed":
        split = next((i for i, line in enumerate(lines) if line.lower().startswith("new shift")), None)
        if split is None:
            raise ValueError("no new shift")
        old, new = _blocks(lines[:split]), _blocks(lines[split:])
        if not old or len(old) != len(new):
            raise ValueError("unpaired change")
        return {"kind": kind, "old": old, "new": new}
    shifts = _blocks(lines)
    if not shifts:
        raise ValueError("no shifts")
    return {"kind": kind, "old": shifts if kind == "removed" else [],
            "new": shifts if kind == "assigned" else []}


def shift_id(start: str, end: str) -> str:
    a, b = dt.datetime.fromisoformat(start), dt.datetime.fromisoformat(end)
    return f"mail-{a:%Y%m%dT%H%M}-{(b - a) // dt.timedelta(minutes=1)}"


def replay(notices: list[dict]) -> tuple[dict, int]:
    """Oldest-first notices -> ({id: [start, end]}, notices that matched nothing)."""
    shifts: dict[str, list[str]] = {}
    unmatched = 0
    def find(pair):
        return next((k for k, v in shifts.items() if v == pair), None)
    for notice in sorted(notices, key=lambda n: (n["at"], n.get("key", ""))):
        if notice["kind"] == "assigned":
            for pair in notice["new"]:
                if find(pair) is None:
                    shifts[shift_id(*pair)] = list(pair)
        elif notice["kind"] == "removed":
            for pair in notice["old"]:
                key = find(pair)
                if key is None:
                    unmatched += 1
                else:
                    del shifts[key]
        else:
            for old, new in zip(notice["old"], notice["new"]):
                key = find(old)
                if key is None:
                    unmatched += 1
                    key = shift_id(*old)
                if (twin := find(new)) is not None and twin != key:
                    del shifts[twin]
                shifts[key] = list(new)
    return shifts, unmatched


def _messages(query: str) -> list[dict]:
    done = subprocess.run(["notmuch", "show", "--format=json", "--include-html",
                           "--entire-thread=false", "--body=true", query],
                          capture_output=True, text=True, timeout=60)
    if done.returncode:
        raise RuntimeError("notmuch unavailable")
    out = []
    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict) and "headers" in node and "body" in node:
            out.append(node)
    walk(json.loads(done.stdout or "[]"))
    return out


def _bodies(parts) -> list[str]:
    found = []
    for part in parts or []:
        content = part.get("content")
        if isinstance(content, list):
            found += _bodies(content)
        elif isinstance(content, str) and str(part.get("content-type", "")).startswith("text/"):
            found.append(content)
    return found


def _state_path() -> Path:
    try:
        from agent_media_core._paths import state_dir
        base = state_dir()
    except Exception:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "agent-media"
    return base / "calendar" / "roster-mail.json"


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) and data.get("version") == 1 else {"version": 1, "notices": {}}
    except (OSError, ValueError):
        return {"version": 1, "notices": {}}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".roster-mail.")
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read(first: dt.date, last: dt.date, query: str = QUERY, *, now: dt.datetime | None = None) -> dict:
    """The `shift_events()` packet, plus `skipped` (recent roster notices that
    could not be read), `unmatched` and `notices`. Raises when notmuch fails."""
    now = now or dt.datetime.now(ZONE)
    path = _state_path()
    state = _load(path)
    known = state["notices"]
    skipped = 0
    for message in _messages(query):
        key = str(message.get("id") or "")
        if not key or key in known:
            continue
        at = int(message.get("timestamp") or 0)
        subject = str(message.get("headers", {}).get("Subject") or "")
        notice, failed = None, False
        for body in _bodies(message.get("body")):
            try:
                notice = parse(text_of(body))
            except ValueError:
                failed = True
                continue
            if notice:
                break
        if notice:
            known[key] = {"at": at, **notice}
        elif failed or _SUBJECT.search(subject):
            if dt.datetime.fromtimestamp(at, ZONE) >= now - RECENT:
                skipped += 1
    shifts, unmatched = replay([{"key": k, **v} for k, v in known.items()])
    _save(path, state)
    return {"ok": True, "complete": True, "start": first.isoformat(), "end": last.isoformat(),
            "shifts": [{"id": k, "start": v[0], "end": v[1]} for k, v in sorted(shifts.items(), key=lambda kv: kv[1])],
            "skipped": skipped, "unmatched": unmatched, "notices": len(known)}
