"""Netbookings staff grid -> private, time-only treatment calendar entries."""
from __future__ import annotations

import datetime as dt
import html
import http.cookiejar
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from . import calendar_access, calendar_write


def parse_grid(body: str, day: dt.date, staff: str) -> list[dict]:
    if not re.search(r'<option\s+data-staff="' + re.escape(staff) + r'"', body):
        raise ValueError("Own staff selector absent; timetable completeness unverified")
    zone = ZoneInfo("Australia/Melbourne")
    out = {}
    for block in re.split(r'(?=<td class="cell )', body):
        match = re.match(r'<td class="cell booked staff-cell"\s+data-reference="(\d+)"\s+data-staff="(\d+)"', block)
        if not match or match[2] != staff:
            continue
        raw = re.search(r'booking-time">(.*?)</span>', block.split('<td class="time')[0], re.S)
        text = html.unescape(re.sub(r"<[^>]+>", "", raw[1])) if raw else ""
        times = re.fullmatch(r'\s*(\d{1,2}:\d{2}\s*[ap]m)\s*[–—-]\s*(\d{1,2}:\d{2}\s*[ap]m)\s*', text, re.I)
        if not times:
            raise ValueError("Treatment time format unrecognised; retaining existing entries")
        def stamp(value):
            clock = dt.datetime.strptime(re.sub(r"\s", "", value).upper(), "%I:%M%p").time()
            return dt.datetime.combine(day, clock, zone)
        start, end = stamp(times[1]), stamp(times[2])
        if end <= start:
            end += dt.timedelta(days=1)
        uid = f"phs-treatment-{day.isoformat()}-{match[1]}-{start:%H%M}@sasonica"
        out[uid] = {"uid": uid, "title": "PHS — treatment", "start": start.isoformat(),
                    "end": end.isoformat(), "source": "phs-treatment", "busy": True,
                    "description": "Treatment time only. No client details. Designated shift finish is tracked separately."}
    return list(out.values())


def sync() -> dict:
    from agent_media_core import config
    settings = config.load().get("calendar", {}).get("phs", {})
    script = Path(str(settings.get("session_command") or "")).expanduser()
    staff = str(settings.get("staff_id") or "")
    if not staff or not script.is_file():
        raise ValueError("PHS calendar source is not configured")
    today = dt.datetime.now(ZoneInfo("Australia/Melbourne")).date()
    days = [today, today + dt.timedelta(days=1)]
    expected = {}
    # Read both complete grids before changing any calendar entry.
    with tempfile.TemporaryDirectory(prefix="phs-calendar-") as folder:
        done = subprocess.run([str(script)], env={**os.environ, "PHS_DIR": folder},
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=100)
        if done.returncode:
            raise RuntimeError("PHS authentication failed; existing calendar entries retained")
        session = (Path(folder) / "session").read_text().strip()
        jar = http.cookiejar.MozillaCookieJar(folder + "/j")
        jar.load(ignore_discard=True, ignore_expires=True)
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        opener.addheaders = [("User-Agent", "Mozilla/5.0"), ("X-Requested-With", "XMLHttpRequest")]
        for day in days:
            query = urllib.parse.urlencode({"gt": 0, "ref": 0, "s": session, "SD": day.strftime("%d/%m/%Y")})
            try:
                with opener.open("https://secure.peninsulahotsprings.com/hotsprings/Availabilitystaff.exe?" + query, timeout=60) as res:
                    body = res.read(8 * 1024 * 1024).decode(errors="replace")
                for event in parse_grid(body, day, staff):
                    expected[event["uid"]] = event
            except Exception:
                raise RuntimeError("PHS timetable incomplete; existing calendar entries retained") from None
    current = calendar_access.query(today.isoformat(), days[-1].isoformat())["events"]
    changed = 0
    for event in expected.values():
        if dt.datetime.fromisoformat(event["end"]) < dt.datetime.now(ZoneInfo("Australia/Melbourne")):
            continue
        result = calendar_write.write(event, sync=False)
        if result.get("exists") and not result.get("matches"):
            calendar_write.write(event, etag=result["etag"], sync=False)
            changed += 1
        elif result.get("created"):
            changed += 1
    # Only this adapter's events, on fully read days, can be cancelled.
    for old in current:
        uid = old["uid"]
        if dt.datetime.fromisoformat(old["start_at"]) < dt.datetime.now(ZoneInfo("Australia/Melbourne")):
            continue
        if not old.get("managed") or old.get("shift_source") != "phs-treatment" or uid in expected:
            continue
        if not any(uid.startswith(f"phs-treatment-{day.isoformat()}-") for day in days):
            continue
        # Obtain the current ETag without changing an existing event.
        probe = {"uid": uid, "title": old["title"], "start": old["start_at"], "end": old["end_at"]}
        result = calendar_write.write(probe, sync=False)
        if result.get("exists"):
            calendar_write.write({"uid": uid}, etag=result["etag"], delete=True, sync=False)
            changed += 1
    pair = config.load().get("calendar", {}).get("write_pair")
    done = subprocess.run(["vdirsyncer", "sync", pair], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=120)
    if done.returncode:
        raise RuntimeError("Treatments updated remotely but local calendar sync failed")
    return {"ok": True, "treatments": len(expected), "changed": changed}
