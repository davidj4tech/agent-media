"""Push timing-only calendar availability to an authenticated booking site."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.request
from zoneinfo import ZoneInfo

from . import calendar_access
from .calendar_write import _NoRedirect


def snapshot(result: dict, now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    if not result["sources"] or not all(s.get("ok") for s in result["sources"]):
        raise ValueError("Calendar sources unavailable; keeping the last published availability")
    zone = ZoneInfo("Australia/Melbourne")
    first, last = dt.date.fromisoformat(result["start"]), dt.date.fromisoformat(result["end"])
    lo = int(dt.datetime.combine(first, dt.time(), zone).timestamp())
    hi = int(dt.datetime.combine(last + dt.timedelta(days=1), dt.time(), zone).timestamp())
    busy, after = [], {}
    for event in result["events"]:
        # The destination already blocks its own bookings from its database.
        # Echoing their feed back as external busy time creates self-conflicts.
        if re.fullmatch(r"rye-booking-\d+@ryeremedial\.com\.au", str(event.get("uid", ""))):
            continue
        start = dt.datetime.fromisoformat(event["start_at"]).astimezone(zone)
        end = dt.datetime.fromisoformat(event["end_at"]).astimezone(zone)
        if end <= start:
            continue
        if event.get("busy", True):
            busy.append([max(lo, int(start.timestamp())), min(hi, int(end.timestamp()))])
        if event.get("managed") and event.get("shift_source") in ("marlo", "phs-shift"):
            # Keep the designated finish even when treatments refine busy time.
            opens = int(end.timestamp()) + 75 * 60
            day = max(start.date(), first)
            while day <= min(end.date(), last):
                key = day.isoformat()
                after[key] = max(after.get(key, 0), opens)
                day += dt.timedelta(days=1)
    return {"version": 1, "generated_at": now, "from": result["start"],
            "to": result["end"], "busy": sorted(set(tuple(v) for v in busy)),
            "open_after": after}


def publish(path: str) -> dict:
    file = Path(path).expanduser()
    if file.stat().st_mode & 0o077:
        raise ValueError("Publishing configuration must be owner-only")
    config = json.loads(file.read_text())
    endpoint = config["endpoint"]
    if not endpoint.startswith("https://"):
        raise ValueError("Availability publishing requires HTTPS")
    from agent_media_core import config as media_config
    if media_config.load().get("calendar", {}).get("rye", {}).get("write_pair"):
        from . import booking_calendar
        booking_calendar.sync(sync_local=False)
    # A fresh remote sync must succeed before the site receives new data.
    done = subprocess.run(["vdirsyncer", "sync"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=180)
    if done.returncode:
        raise RuntimeError("Calendar sync failed; last published availability retained")
    first = dt.datetime.now(ZoneInfo("Australia/Melbourne")).date()
    result = calendar_access.query(first.isoformat(), (first + dt.timedelta(days=40)).isoformat())
    data = snapshot(result)
    req = urllib.request.Request(endpoint, data=json.dumps(data).encode(), method="POST",
        headers={"Authorization": "Bearer " + config["token"], "Content-Type": "application/json",
                 "User-Agent": "Sasonica-Calendar/1.0"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=30) as response:
            answer = json.loads(response.read(4096))
        if not answer.get("ok"):
            raise RuntimeError()
    except Exception:
        raise RuntimeError("Availability publication failed; no private response logged") from None
    return {"ok": True, "busy_intervals": len(data["busy"]), "shift_days": len(data["open_after"]),
            "booking_conflicts": int(answer.get("booking_conflicts", 0))}
