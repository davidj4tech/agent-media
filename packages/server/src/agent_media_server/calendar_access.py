"""Shared, read-only calendar queries for local owner harnesses."""

from __future__ import annotations

import datetime as dt


def query(start: str = "", end: str = "", search: str = "") -> dict:
    """Read configured calendars; never return private feed URLs or paths."""
    from . import calendar_sources

    first = dt.date.fromisoformat(start) if start else dt.date.today()
    last = dt.date.fromisoformat(end) if end else first + dt.timedelta(days=6)
    if last < first or (last - first).days > 366:
        raise ValueError("Calendar range must be ordered and at most 367 days inclusive")
    rows, sources = calendar_sources.events(first, last)
    if search:
        needle = search.casefold()
        rows = [row for row in rows if needle in " ".join(
            str(row.get(key) or "") for key in ("title", "location")
        ).casefold()]
    rows.sort(key=lambda row: (row["date"], row.get("time", ""), row["title"]))
    return {"start": first.isoformat(), "end": last.isoformat(),
            "timezone": str(dt.datetime.now().astimezone().tzinfo),
            "read_only": True, "events": rows, "sources": sources,
            "freshness_note": "For local calendars, synced is the newest event file modification, not proof of a successful remote sync."}
