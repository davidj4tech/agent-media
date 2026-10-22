"""Owner-local CalDAV writes using the existing vdirsyncer credential.

Only resources created by this interface are mutable. Conditional requests
protect concurrent calendar edits; credentials and remote URLs stay private.
"""
from __future__ import annotations

import base64
import configparser
import datetime as dt
import json
import re
from pathlib import Path
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo


class CalendarWriteError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _connection(pair: str):
    from agent_media_core import config
    settings = config.load().get("calendar", {})
    pair = pair or settings.get("write_pair", "")
    if not pair:
        raise CalendarWriteError("No writable calendar pair configured")
    path = Path(settings.get("vdirsyncer_config", "~/.config/vdirsyncer/config")).expanduser()
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path)
        remote = json.loads(parser[f"pair {pair}"]["b"])
        storage = parser[f"storage {remote}"]
        if json.loads(storage.get("read_only", "false")):
            raise ValueError("read-only")
        if json.loads(storage["type"]) != "caldav":
            raise ValueError("not CalDAV")
        url = json.loads(storage["url"])
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("HTTPS collection URL required")
        fetch = json.loads(storage["password.fetch"])
        if not isinstance(fetch, list) or len(fetch) < 2 or fetch[0] != "command":
            raise ValueError("command credential required")
        secret = subprocess.check_output(fetch[1:], stderr=subprocess.DEVNULL, timeout=20).decode().strip()
        user = json.loads(storage["username"])
        auth = "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()
        return pair, url.rstrip("/") + "/", auth
    except Exception:
        raise CalendarWriteError("Calendar connection configuration or credential unavailable") from None


def _request(url, auth, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Authorization": auth, **(headers or {})})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=30) as res:
            status, data, tag = res.status, res.read(2 * 1024 * 1024), res.headers.get("ETag", "")
        if method == "GET" and not tag:
            _, props, _ = _request(url, auth, "PROPFIND",
                b'<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/></d:prop></d:propfind>',
                {"Depth": "0", "Content-Type": "application/xml"})
            tag = ET.fromstring(props).findtext(".//{DAV:}getetag") or ""
        return status, data, tag
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and method == "GET":
            return 404, b"", ""
        if exc.code == 412:
            raise CalendarWriteError("Calendar event changed; read it again before editing") from None
        raise CalendarWriteError(f"Calendar request failed (HTTP {exc.code})") from None
    except Exception:
        raise CalendarWriteError("Calendar request failed; no credentials or server response logged") from None


def _body(event: dict) -> tuple[str, bytes]:
    from icalendar import Calendar, Event
    uid = str(event.get("uid") or uuid.uuid4().hex + "@sasonica")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,199}", uid):
        raise ValueError("Invalid event UID")
    zone = ZoneInfo(event.get("timezone") or "Australia/Melbourne")
    all_day = bool(event.get("all_day", False))
    def parse(value):
        if all_day:
            return dt.date.fromisoformat(value)
        result = dt.datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError("Timed events require explicit UTC offsets")
        return result.astimezone(zone)
    start, end = parse(event["start"]), parse(event["end"])
    if end <= start:
        raise ValueError("Event end must be after start (exclusive for all-day events)")
    title = str(event.get("title") or "").strip()
    if not title:
        raise ValueError("Event title required")
    cal, ev = Calendar(), Event()
    cal.add("prodid", "-//Sasonica//Shared Calendar//EN")
    cal.add("version", "2.0")
    for key, value in [("uid", uid), ("summary", title), ("dtstart", start),
                       ("dtend", end), ("dtstamp", dt.datetime.now(dt.timezone.utc)),
                       ("transp", "OPAQUE" if event.get("busy", True) else "TRANSPARENT"),
                       ("x-sasonica-managed", "1")]:
        ev.add(key, value)
    for key in ("description", "location"):
        if event.get(key):
            ev.add(key, str(event[key]))
    if event.get("status"):
        status = str(event["status"]).upper()
        if status not in ("CONFIRMED", "TENTATIVE"):
            raise ValueError("Invalid event status")
        ev.add("status", status)
    if event.get("source"):
        if event["source"] not in ("marlo", "phs-shift", "phs-treatment", "rye-booking"):
            raise ValueError("Unknown managed shift source")
        ev.add("x-sasonica-source", event["source"])
    if event.get("rrule"):
        from icalendar import vRecur
        from dateutil.rrule import rrulestr
        rule = str(event["rrule"]).removeprefix("RRULE:")
        rrulestr(rule, dtstart=start if not all_day else dt.datetime.combine(start, dt.time()))
        ev.add("rrule", vRecur.from_ical(rule))
    cal.add_component(ev)
    return uid, cal.to_ical()


def inspect_event(uid: str, calendar: str = "") -> dict:
    """Read an owned resource without ever recreating a missing event."""
    from icalendar import Calendar
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,199}", uid):
        raise ValueError("Invalid event UID")
    _, base, auth = _connection(calendar)
    status, body, etag = _request(base + urllib.parse.quote(uid, safe="") + ".ics", auth)
    if status == 404:
        return {"uid": uid, "exists": False}
    events = Calendar.from_ical(body).walk("VEVENT")
    if len(events) != 1 or str(events[0].get("UID")) != uid or str(events[0].get("X-SASONICA-MANAGED")) != "1":
        raise CalendarWriteError("Refusing to inspect an event not managed by Sasonica")
    return {"uid": uid, "exists": True, "etag": etag}


def write(event: dict, calendar: str = "", etag: str = "", delete: bool = False, *, sync: bool = True) -> dict:
    """Create/update/delete an owned event. Updates/deletes require its ETag."""
    from icalendar import Calendar
    if delete:
        uid = str(event.get("uid") or "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,199}", uid) or not etag:
            raise ValueError("Delete requires UID and current ETag")
        body = None
    else:
        uid, body = _body(event)
    pair, base, auth = _connection(calendar)
    # Stable resource name also makes retrying an interrupted create safe.
    url = base + urllib.parse.quote(uid, safe="") + ".ics"
    status, old, current = _request(url, auth)
    if status != 404:
        try:
            events = Calendar.from_ical(old).walk("VEVENT")
            owned = len(events) == 1 and str(events[0].get("UID")) == uid and str(events[0].get("X-SASONICA-MANAGED")) == "1"
        except Exception:
            owned = False
        if not owned:
            raise CalendarWriteError("Refusing to modify an event not managed by Sasonica")
        if not etag:
            proposed = Calendar.from_ical(body).walk("VEVENT")[0]
            def comparable(ev):
                return {str(k): v.to_ical() if hasattr(v, "to_ical") else str(v)
                        for k, v in ev.items() if str(k).upper() not in
                        {"DTSTAMP", "CREATED", "LAST-MODIFIED", "SEQUENCE"}}
            return {"uid": uid, "etag": current, "created": False, "exists": True,
                    "matches": comparable(events[0]) == comparable(proposed),
                    "message": "Event already exists; supply its ETag to update"}
        if etag != current:
            raise CalendarWriteError("Calendar event changed; read it again before editing")
    elif etag or delete:
        raise CalendarWriteError("Calendar event no longer exists")
    headers = {"If-Match": etag} if etag else {"If-None-Match": "*"}
    headers["Content-Type"] = "text/calendar; charset=utf-8"
    _request(url, auth, "DELETE" if delete else "PUT", body, headers)
    new_etag = ""
    if delete:
        if _request(url, auth)[0] != 404:
            raise CalendarWriteError("Delete completed but absence could not be verified")
    else:
        verified, saved, new_etag = _request(url, auth)
        if verified != 200 or not any(str(e.get("UID")) == uid for e in Calendar.from_ical(saved).walk("VEVENT")):
            raise CalendarWriteError("Write completed but calendar read-back could not be verified")
    # Download canonical server data; never put a competing copy in the vdir.
    if not sync:
        return {"uid": uid, "etag": new_etag, "created": status == 404,
                "deleted": delete, "verified": True, "local_sync_ok": False}
    try:
        result = subprocess.run(["vdirsyncer", "sync", pair], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=120)
        synced = result.returncode == 0
    except Exception:
        synced = False
    return {"uid": uid, "etag": new_etag, "created": status == 404,
            "deleted": delete, "verified": True, "local_sync_ok": synced}
