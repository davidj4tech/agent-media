"""Mirror the complete private Rye booking feed into its Google calendar."""
from __future__ import annotations

import configparser
import datetime as dt
import json
from pathlib import Path
import re
import subprocess
import urllib.request

from . import calendar_write

UID = re.compile(r"rye-booking-\d+@ryeremedial\.com\.au")
MAX_BYTES = 20 * 1024 * 1024


def parse_feed(body: bytes) -> list[dict]:
    """Validate the full response before permitting any remote mutation."""
    from icalendar import Calendar
    if not body.strip().endswith(b"END:VCALENDAR"):
        raise ValueError("Incomplete booking feed")
    cal = Calendar.from_ical(body)
    if str(cal.get("PRODID")) != "-//Rye Remedial//Bookings//EN" or str(cal.get("METHOD")) != "PUBLISH":
        raise ValueError("Unexpected booking feed")
    events = {}
    for ev in cal.walk("VEVENT"):
        uid = str(ev.get("UID") or "")
        start, end = ev.decoded("DTSTART"), ev.decoded("DTEND")
        status = str(ev.get("STATUS") or "")
        title = str(ev.get("SUMMARY") or "").strip()
        if (not UID.fullmatch(uid) or uid in events or not title
                or not isinstance(start, dt.datetime) or not isinstance(end, dt.datetime)
                or start.tzinfo is None or end.tzinfo is None or end <= start
                or status not in ("CONFIRMED", "TENTATIVE") or ev.get("RRULE")):
            raise ValueError("Invalid or duplicate booking event")
        events[uid] = {"uid": uid, "title": title, "start": start.isoformat(), "end": end.isoformat(),
                       "status": status, "location": str(ev.get("LOCATION") or ""),
                       "source": "rye-booking", "busy": True,
                       "description": "Synced from Rye Remedial bookings. Change this booking on the website."}
    return list(events.values())


def _read(url: str) -> list[dict]:
    try:
        if not url.startswith("https://ryeremedial.com.au/book/calendar/"):
            raise ValueError()
        req = urllib.request.Request(url, headers={"User-Agent": "Sasonica-Calendar/1.0"})
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError()
        return parse_feed(body)
    except Exception:
        raise RuntimeError("Booking feed unavailable or incomplete; Google entries retained") from None


def _local_owned(pair: str) -> list[dict]:
    """Only this adapter's entries in this pair's successfully synced folder."""
    from icalendar import Calendar
    from agent_media_core import config
    settings = config.load().get("calendar", {})
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(Path(settings.get("vdirsyncer_config", "~/.config/vdirsyncer/config")).expanduser())
    local = parser["storage " + json.loads(parser["pair " + pair]["a"])]
    if json.loads(local["type"]) != "filesystem":
        raise ValueError("Booking sync requires a local filesystem calendar")
    folder = Path(json.loads(local["path"])).expanduser()
    if not folder.is_dir():
        raise ValueError("Booking calendar local folder unavailable")
    result = []
    for path in folder.glob("*.ics"):
        for ev in Calendar.from_ical(path.read_bytes()).walk("VEVENT"):
            uid = str(ev.get("UID") or "")
            if (UID.fullmatch(uid) and str(ev.get("X-SASONICA-MANAGED")) == "1"
                    and str(ev.get("X-SASONICA-SOURCE")) == "rye-booking"):
                start = ev.decoded("DTSTART")
                if not isinstance(start, dt.datetime) or start.tzinfo is None:
                    raise ValueError("Invalid managed booking time")
                result.append({"uid": uid, "start": start})
    return result


def _sync_local(pair: str) -> None:
    done = subprocess.run(["vdirsyncer", "sync", pair], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=120)
    if done.returncode:
        raise RuntimeError("Booking calendar sync failed; retry required")


def sync(*, sync_local: bool = True) -> dict:
    from agent_media_core import config
    settings = config.load().get("calendar", {}).get("rye", {})
    pair, url = str(settings.get("write_pair") or ""), str(settings.get("feed_url") or "")
    if not pair or not url:
        raise ValueError("Rye booking calendar destination is not configured")
    events = _read(url)
    _sync_local(pair)
    old = _local_owned(pair)
    keep, changed = {e["uid"] for e in events}, 0
    for event in events:
        current = calendar_write.write(event, calendar=pair, sync=False)
        if current.get("exists") and not current.get("matches"):
            calendar_write.write(event, calendar=pair, etag=current["etag"], sync=False)
            changed += 1
        elif current.get("created"):
            changed += 1
    # A missing future booking means cancellation, but only after a complete read.
    now = dt.datetime.now(dt.timezone.utc)
    for event in old:
        if event["uid"] in keep or event["start"] <= now:
            continue
        current = calendar_write.inspect_event(event["uid"], calendar=pair)
        if current.get("exists"):
            calendar_write.write({"uid": event["uid"]}, calendar=pair,
                                 etag=current["etag"], delete=True, sync=False)
            changed += 1
    if sync_local:
        _sync_local(pair)
    return {"ok": True, "bookings": len(events), "changed": changed}
