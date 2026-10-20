"""Calendars beside the Org items: events from the sources an install names.

config.toml, one table per source:

    [[calendar.sources]]
    kind = "vdir"                     # a folder of .ics files, one item each,
    path = "~/.calendars/work"        #   as vdirsyncer, khal and others keep;
    name = "Work"                     #   a folder of such folders is one
    color = "#3a7"                    #   calendar per folder
    [[calendar.sources]]
    kind = "ics"                      # a calendar's published address
    url = "https://…/basic.ics"       #   (Google's "secret address",
    name = "Family"                   #   Outlook's published calendar)

`name` and `color` are optional: a vdir folder's own `displayname` and
`color` files (vdirsyncer's metadata) come next, then the folder's name. An
ICS link is fetched at most every `refresh_min` minutes (15), into
<state_dir>/calendar/, and the last good copy stands while it cannot be
fetched. A link is a secret: it is never logged or sent to the app. These are the
owner's calendars: an account paired with this server sees none of them.

Each event becomes an item for org_calendar: `type: "event"`, `source:
"calendar"`, `calendar: {id, name, color}`, `uid`, `title`, `location`,
`date`, `time`/`end_time` (local to this server), `end_date` for one that
runs past its first day, `all_day`. Repeats (RRULE, RDATE, EXDATE) are
expanded inside the range, an occurrence moved or cancelled on its own
(RECURRENCE-ID) replacing the one it stands for. Read only for now:
docs/proposals/2026-10-20-organiser-calendar.md.

Parsing is the icalendar library (BSD), imported when first needed, so a
server without it still runs and reports its sources as unavailable.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

REFRESH_MIN = 15
FETCH_TIMEOUT_S = 15
MAX_ICS_BYTES = 20 * 1024 * 1024
#: Occurrences of one repeating event in one range, at most.
MAX_REPEATS = 500

_LOCK = threading.Lock()
#: path → (mtime, size, [VEVENT components]) for vdir files and fetched links.
_PARSED: dict[str, tuple[float, int, list]] = {}
_FETCHING: set[str] = set()


def _config() -> list[dict]:
    try:
        from agent_media_core import config
        got = config.load().get("calendar") or {}
    except Exception:  # noqa: BLE001 — a bad config file means "nothing set"
        return []
    raw = got.get("sources") if isinstance(got, dict) else None
    return [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []


def _state_dir() -> Path:
    from agent_media_core._paths import state_dir

    return state_dir() / "calendar"


def _id(kind: str, key: str) -> str:
    return f"{kind}-{hashlib.sha1(key.encode()).hexdigest()[:10]}"


def _read_meta(folder: Path, name: str) -> str:
    try:
        return (folder / name).read_text(errors="replace").strip()
    except OSError:
        return ""


def calendars() -> list[dict]:
    """Every calendar the config names, each {id, name, color, kind, and
    path (vdir) or url (ics, never sent on)}."""
    out = []
    for src in _config():
        kind = str(src.get("kind") or "").lower()
        if kind == "vdir" and src.get("path"):
            base = Path(str(src["path"])).expanduser()
            folders = [base]
            if base.is_dir() and not any(base.glob("*.ics")):
                subs = sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))
                if subs:
                    folders = subs
            for folder in folders:
                name = (src.get("name") if folder == base else None) or _read_meta(folder, "displayname") or folder.name
                color = (src.get("color") if folder == base else None) or _read_meta(folder, "color") or src.get("color") or ""
                out.append({"id": _id("vdir", str(folder)), "kind": "vdir", "name": str(name),
                            "color": str(color), "path": folder})
        elif kind == "ics" and src.get("url"):
            url = str(src["url"])
            try:
                every = max(1, int(src.get("refresh_min") or REFRESH_MIN))
            except (TypeError, ValueError):
                every = REFRESH_MIN
            out.append({"id": _id("ics", url), "kind": "ics", "name": str(src.get("name") or "Calendar"),
                        "color": str(src.get("color") or ""), "url": url, "refresh_min": every})
    return out


# --- reading ---------------------------------------------------------------------------

def _icalendar():
    try:
        import icalendar
        return icalendar
    except ImportError:
        return None


def _parse_file(path: Path) -> list:
    """The VEVENTs in one .ics file, parsed once per change."""
    try:
        st = path.stat()
    except OSError:
        return []
    key = str(path)
    with _LOCK:
        hit = _PARSED.get(key)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]
    ical = _icalendar()
    events = []
    try:
        cal = ical.Calendar.from_ical(path.read_bytes())
        events = [c for c in cal.walk("VEVENT")]
    except Exception as e:  # noqa: BLE001 — one bad file costs only itself
        log.info("calendar: skipping %s (%s)", path.name, type(e).__name__)
    with _LOCK:
        _PARSED[key] = (st.st_mtime, st.st_size, events)
    return events


def _fetch(cal: dict, dest: Path) -> None:
    try:
        req = urllib.request.Request(cal["url"].replace("webcal://", "https://", 1),
                                     headers={"User-Agent": "agent-media calendar"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as res:
            body = res.read(MAX_ICS_BYTES + 1)
        if len(body) > MAX_ICS_BYTES or b"BEGIN:VCALENDAR" not in body[:4096]:
            raise ValueError("not a calendar")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        os.replace(tmp, dest)
    except Exception as e:  # noqa: BLE001 — the last good copy stands
        log.info("calendar: %s could not be fetched (%s)", cal["name"], type(e).__name__)
    finally:
        with _LOCK:
            _FETCHING.discard(cal["id"])


def _ics_events(cal: dict) -> tuple[list, dict]:
    """A link's events from its last good copy, fetching a newer one: in the
    background when there is a copy, while the caller waits when there is
    none yet."""
    dest = _state_dir() / f"{cal['id']}.ics"
    try:
        age = time.time() - dest.stat().st_mtime
    except OSError:
        age = None
    if age is None or age > cal["refresh_min"] * 60:
        with _LOCK:
            start = cal["id"] not in _FETCHING
            _FETCHING.add(cal["id"])
        if start:
            if age is None:
                _fetch(cal, dest)
            else:
                threading.Thread(target=_fetch, args=(cal, dest), daemon=True).start()
    try:
        synced = dest.stat().st_mtime
    except OSError:
        return [], {"ok": False, "error": "not fetched yet"}
    return _parse_file(dest), {"ok": True, "synced": round(synced)}


def _vdir_events(cal: dict) -> tuple[list, dict]:
    folder: Path = cal["path"]
    if not folder.is_dir():
        return [], {"ok": False, "error": "no such folder"}
    events, newest = [], 0.0
    for p in folder.glob("*.ics"):
        events += _parse_file(p)
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            pass
    return events, {"ok": True, **({"synced": round(newest)} if newest else {})}


# --- expanding -------------------------------------------------------------------------

def _local(value) -> dt.datetime | dt.date:
    """A DTSTART-like value as a local datetime (or a date for all day)."""
    if isinstance(value, dt.datetime):
        return value.astimezone().replace(tzinfo=None) if value.tzinfo else value
    return value


def _occurrences(ev, start: dt.datetime, end: dt.datetime) -> list:
    """The starts of one VEVENT's occurrences that may touch [start, end),
    in the event's own zone."""
    from dateutil import rrule

    first = ev.decoded("DTSTART")
    if "RRULE" not in ev and "RDATE" not in ev:
        return [first]
    all_day = not isinstance(first, dt.datetime)
    anchor = dt.datetime.combine(first, dt.time()) if all_day else first
    rules = rrule.rruleset()
    raw_rules = ev.get("RRULE")
    for rr in raw_rules if isinstance(raw_rules, list) else [raw_rules] if raw_rules is not None else []:
        text = rr.to_ical().decode()
        if anchor.tzinfo is None:
            text = re.sub(r"(UNTIL=\d{8}(?:T\d{6})?)Z", r"\1", text)
        elif "UNTIL=" in text and not re.search(r"UNTIL=\d{8}T\d{6}Z", text):
            text = re.sub(r"UNTIL=(\d{8})(?:T(\d{6}))?", lambda m: f"UNTIL={m.group(1)}T{m.group(2) or '235959'}Z", text)
        try:
            rules.rrule(rrule.rrulestr(text, dtstart=anchor))
        except (ValueError, TypeError):
            return [first]
    for key, add in (("RDATE", rules.rdate), ("EXDATE", rules.exdate)):
        raw = ev.get(key)
        for group in raw if isinstance(raw, list) else [raw] if raw is not None else []:
            for d in getattr(group, "dts", []):
                v = d.dt
                if not isinstance(v, dt.datetime):
                    v = dt.datetime.combine(v, dt.time())
                if anchor.tzinfo is None and v.tzinfo is not None:
                    v = v.astimezone().replace(tzinfo=None)
                elif anchor.tzinfo is not None and v.tzinfo is None:
                    v = v.replace(tzinfo=anchor.tzinfo)
                add(v)
    lo, hi = start, end
    if anchor.tzinfo is not None:
        lo, hi = start.astimezone(), end.astimezone()
    out = []
    for occ in rules.between(lo, hi, inc=True):
        out.append(occ.date() if all_day else occ)
        if len(out) >= MAX_REPEATS:
            break
    return out


def _span(ev, first) -> dt.timedelta:
    if "DTEND" in ev:
        return ev.decoded("DTEND") - ev.decoded("DTSTART")
    if "DURATION" in ev:
        return ev.decoded("DURATION")
    return dt.timedelta(days=1) if not isinstance(first, dt.datetime) else dt.timedelta()


def _item(cal: dict, ev, occ, length: dt.timedelta) -> dict:
    row = {"type": "event", "source": "calendar", "overdue": False,
           "calendar": {"id": cal["id"], "name": cal["name"], "color": cal["color"]},
           "uid": str(ev.get("UID") or ""), "title": str(ev.get("SUMMARY") or "").strip() or "(no title)",
           "state": "", "priority": "", "tags": [], "level": 1}
    if loc := str(ev.get("LOCATION") or "").strip():
        row["location"] = loc
    if isinstance(occ, dt.datetime):
        s = _local(occ)
        e = _local(occ + length)
        row.update(date=s.date().isoformat(), time=s.strftime("%H:%M"), all_day=False)
        if e > s:
            row["end_time"] = e.strftime("%H:%M")
            last = (e - dt.timedelta(microseconds=1)).date()
            if last > s.date():
                row["end_date"] = last.isoformat()
    else:
        row.update(date=occ.isoformat(), all_day=True)
        last = occ + max(length, dt.timedelta(days=1)) - dt.timedelta(days=1)
        if last > occ:
            row["end_date"] = last.isoformat()
    return row


def _overlaps(row: dict, start: dt.date, end: dt.date) -> bool:
    first = dt.date.fromisoformat(row["date"])
    last = dt.date.fromisoformat(row.get("end_date") or row["date"])
    return first <= end and last >= start


def _rid(value) -> str:
    v = _local(value)
    return v.isoformat() if isinstance(v, dt.datetime) else dt.datetime.combine(v, dt.time()).isoformat()


def events(start: dt.date, end: dt.date) -> tuple[list[dict], list[dict]]:
    """Every source's events touching [start, end], and the calendars as the
    app may see them: {id, name, color, kind, ok, synced?, error?}."""
    cals = calendars()
    if not cals:
        return [], []
    if _icalendar() is None:
        return [], [{**_public(c), "ok": False, "error": "the icalendar package is not installed"}
                    for c in cals]
    lo = dt.datetime.combine(start, dt.time())
    hi = dt.datetime.combine(end + dt.timedelta(days=1), dt.time())
    items: list[dict] = []
    shown: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for cal in cals:
        comps, status = _ics_events(cal) if cal["kind"] == "ics" else _vdir_events(cal)
        shown.append({**_public(cal), **status})
        moved: dict[str, set[str]] = {}
        for ev in comps:
            if "RECURRENCE-ID" in ev:
                moved.setdefault(str(ev.get("UID")), set()).add(_rid(ev.decoded("RECURRENCE-ID")))
        for ev in comps:
            try:
                if "DTSTART" not in ev or str(ev.get("STATUS") or "").upper() == "CANCELLED":
                    continue
                first = ev.decoded("DTSTART")
                length = _span(ev, first)
                reach_lo = lo - max(length, dt.timedelta())
                skip = moved.get(str(ev.get("UID")), set()) if "RECURRENCE-ID" not in ev else set()
                for occ in _occurrences(ev, reach_lo, hi):
                    if skip and _rid(occ) in skip:
                        continue
                    row = _item(cal, ev, occ, length)
                    key = (row["uid"], row["date"], row.get("time", ""))
                    if _overlaps(row, start, end) and key not in seen:
                        seen.add(key)
                        items.append(row)
            except Exception as e:  # noqa: BLE001 — one odd event costs only itself
                log.info("calendar: an event in %s skipped (%s)", cal["name"], type(e).__name__)
    return items, shown


def _public(cal: dict) -> dict:
    return {"id": cal["id"], "name": cal["name"], "color": cal["color"], "kind": cal["kind"]}
