"""Keep one managed source's events in a calendar in step with a complete list.

An outside reader (a roster, a staff timetable, any feed this repo should not
know about) prints the full set of events it owns for a date range; this
writes the new and changed ones and removes the source's own entries that
have gone. Only events tagged with the same source, inside the range and not
yet started, can be removed, so a reader cannot touch anything else. Events
already finished are left as they were. A reader that fails should not call
this at all: an empty list means "nothing in the range" and removes.
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess

from . import calendar_access, calendar_write

SOURCE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def reconcile(events: list[dict], *, source: str, calendar: str, start: str, end: str,
              refuse_empty: bool = False, sync: bool = True, now: dt.datetime | None = None) -> dict:
    if not SOURCE.fullmatch(source):
        raise ValueError("--source: lowercase letters, digits and dashes")
    if not calendar:
        raise ValueError("--calendar PAIR required")
    first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    if last < first:
        raise ValueError("--end before --start")
    now = now or dt.datetime.now(dt.timezone.utc).astimezone()
    wanted = {}
    for event in events:
        if not isinstance(event, dict) or not event.get("uid"):
            raise ValueError("Every event needs a uid")
        if event.get("source", source) != source:
            raise ValueError("Event source differs from --source")
        if event["uid"] in wanted and wanted[event["uid"]] != event:
            raise ValueError("Conflicting events share a uid")
        wanted[event["uid"]] = {**event, "source": source}
    old = [e for e in calendar_access.query(first.isoformat(), last.isoformat())["events"]
           if e.get("managed") and e.get("shift_source") == source]
    if refuse_empty and not wanted and old:
        raise RuntimeError("Empty list with entries present; existing entries retained")
    changed = 0
    for event in wanted.values():
        if dt.datetime.fromisoformat(event["end"]) < now:
            continue
        current = calendar_write.write(event, calendar=calendar, sync=False)
        if current.get("exists") and not current.get("matches"):
            calendar_write.write(event, calendar=calendar, etag=current["etag"], sync=False)
            changed += 1
        elif current.get("created"):
            changed += 1
    removed = 0
    for event in old:
        if event["uid"] in wanted or dt.datetime.fromisoformat(event["start_at"]) <= now:
            continue
        if not first <= dt.datetime.fromisoformat(event["start_at"]).date() <= last:
            continue
        # Obtain the current ETag without changing an existing event.
        current = calendar_write.inspect_event(event["uid"], calendar=calendar)
        if current.get("exists"):
            calendar_write.write({"uid": event["uid"]}, calendar=calendar, etag=current["etag"],
                                 delete=True, sync=False)
            removed += 1
    if sync:
        done = subprocess.run(["vdirsyncer", "sync", calendar], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=120)
        if done.returncode:
            raise RuntimeError("Calendar updated remotely but local calendar sync failed")
    return {"ok": True, "source": source, "events": len(wanted), "changed": changed, "removed": removed}
