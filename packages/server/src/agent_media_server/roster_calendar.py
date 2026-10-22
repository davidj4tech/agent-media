"""Published assigned PHS shifts, with near-term treatment refinement."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import re
import subprocess
from zoneinfo import ZoneInfo

from . import calendar_access, calendar_write, phs_calendar, roster_mail

ZONE = ZoneInfo("Australia/Melbourne")


def shift_events(data: dict, first: dt.date, last: dt.date, refined: bool) -> list[dict]:
    """Use StartTime/EndTime, not the roster UI's misleading allDay flag."""
    if (data.get("ok") is not True or data.get("complete") is not True
            or data.get("start") != first.isoformat() or data.get("end") != last.isoformat()
            or not isinstance(data.get("shifts"), list)):
        raise ValueError("Incomplete or mismatched published roster range")
    result = {}
    for row in data["shifts"]:
        identity = str(row.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identity):
            raise ValueError("Invalid roster identity")
        def parse(value):
            stamp = dt.datetime.fromisoformat(value)
            return stamp.replace(tzinfo=ZONE) if stamp.tzinfo is None else stamp.astimezone(ZONE)
        start, end = parse(row["start"]), parse(row["end"])
        if end <= start:
            raise ValueError("Invalid published shift interval")
        if not first <= start.date() <= last:
            continue
        event = {"uid": f"phs-shift-{identity}@sasonica", "title": "PHS — designated shift",
                 "start": start.isoformat(), "end": end.isoformat(), "source": "phs-shift",
                 "busy": not (refined and start.date() <= first + dt.timedelta(days=1)),
                 "description": "Published designated working hours from Employment Hero. Rye Remedial opens 75 minutes after this shift ends. Near-term treatment appointments carry their own busy time."}
        if event["uid"] in result and result[event["uid"]] != event:
            raise ValueError("Conflicting published shift identities")
        result[event["uid"]] = event
    return list(result.values())


def _browser_roster(reader: Path, node: Path, first: dt.date, last: dt.date) -> dict:
    """The Employment Hero browser reader (a saved portal session)."""
    done = subprocess.run([str(node), str(reader), "--start", first.isoformat(), "--end", last.isoformat()],
                          capture_output=True, text=True, timeout=90)
    if done.returncode:
        raise ValueError()
    return json.loads(done.stdout)


def sync() -> dict:
    from agent_media_core import config
    settings = config.load().get("calendar", {}).get("phs", {})
    pair = str(settings.get("write_pair") or "")
    source = str(settings.get("roster_source") or "browser")
    reader = Path(str(settings.get("roster_command") or "")).expanduser()
    node = Path(str(settings.get("node") or "")).expanduser()
    if not pair or source not in ("browser", "mail") or (
            source == "browser" and not (reader.is_file() and node.is_file())):
        raise ValueError("Published roster reader is not configured")
    now = dt.datetime.now(ZONE)
    first, last = now.date(), now.date() + dt.timedelta(days=14)
    try:
        if source == "mail":
            data = roster_mail.read(first, last, str(settings.get("roster_mail_query") or roster_mail.QUERY))
        else:
            data = _browser_roster(reader, node, first, last)
        events = shift_events(data, first, last, refined=False)
    except Exception:
        raise RuntimeError("Published roster unavailable; previous calendar entries retained") from None
    old = [e for e in calendar_access.query(first.isoformat(), last.isoformat())["events"]
           if e.get("managed") and e.get("shift_source") == "phs-shift"]
    if not events and old:
        raise RuntimeError("Published roster is unexpectedly empty; previous shifts retained")
    # Refine only after complete treatment reads; a failure keeps shift blocks busy.
    treatment_ok = False
    treatment_count = 0
    try:
        treatments = phs_calendar.sync()
        treatment_ok = treatments.get("ok") is True
        treatment_count = treatments.get("treatments", 0)
    except Exception:
        pass
    events = shift_events(data, first, last, refined=treatment_ok)
    keep, changed = set(), 0
    for event in events:
        keep.add(event["uid"])
        if dt.datetime.fromisoformat(event["end"]) < now:
            continue
        current = calendar_write.write(event, calendar=pair, sync=False)
        if current.get("exists") and not current.get("matches"):
            calendar_write.write(event, calendar=pair, etag=current["etag"], sync=False)
            changed += 1
        elif current.get("created"):
            changed += 1
    # Only this source's future shifts in the verified full range can disappear.
    # Switching to mail ids replaces the browser reader's entries (uid
    # phs-shift-<EH id>) once; a legacy entry already under way goes only
    # when a mail shift with the same start replaces it, so none shows twice.
    starts = {dt.datetime.fromisoformat(e["start"]) for e in events}
    for event in old:
        if event["uid"] in keep:
            continue
        start = dt.datetime.fromisoformat(event["start_at"])
        legacy = source == "mail" and not event["uid"].startswith("phs-shift-mail-")
        if start <= now and not (legacy and start in starts and
                                 dt.datetime.fromisoformat(event.get("end_at") or event["start_at"]) > now):
            continue
        current = calendar_write.inspect_event(event["uid"], calendar=pair)
        if current.get("exists"):
            calendar_write.write({"uid": event["uid"]}, calendar=pair, etag=current["etag"], delete=True, sync=False)
            changed += 1
    done = subprocess.run(["vdirsyncer", "sync", pair], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=120)
    if done.returncode:
        raise RuntimeError("Roster updated remotely but local calendar sync failed")
    result = {"ok": True, "roster_source": source, "roster_shifts": len(events),
              "treatments": treatment_count, "treatment_sync_ok": treatment_ok, "changed": changed}
    if source == "mail":
        # Counts only: never names, addresses or bodies.
        result |= {"roster_mail_skipped": data.get("skipped", 0),
                   "roster_mail_notices": data.get("notices", 0)}
    return result
