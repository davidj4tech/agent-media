"""Shared harness access uses fake calendars, never real accounts."""

import datetime as dt

import pytest

from agent_media_server import calendar_access, calendar_sources


def test_query_recurring_free_marker_and_busy_shift(tmp_path, monkeypatch):
    (tmp_path / "shift.ics").write_text("""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:marlo-test
SUMMARY:Marlo shift
DTSTART:20261027T094500
DTEND:20261027T134500
RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=3
END:VEVENT
BEGIN:VEVENT
UID:week-test
SUMMARY:Spa week
DTSTART;VALUE=DATE:20261027
DTEND;VALUE=DATE:20261103
TRANSP:TRANSPARENT
END:VEVENT
END:VCALENDAR
""")
    monkeypatch.setattr(calendar_sources, "_config", lambda: [
        {"kind": "vdir", "path": str(tmp_path), "name": "Test"}])
    result = calendar_access.query("2026-10-27", "2026-11-10")
    assert [(r["date"], r["busy"]) for r in result["events"]] == [
        ("2026-10-27", False), ("2026-10-27", True), ("2026-11-10", True)]
    assert "path" not in result["sources"][0]
    assert "url" not in result["sources"][0]
    assert len(calendar_access.query("2026-10-27", "2026-11-10", "MARLO")["events"]) == 2


@pytest.mark.parametrize("start,end", [("bad", ""), ("2026-11-01", "2026-10-01"),
                                        ("2026-01-01", "2028-01-01")])
def test_bad_range_does_not_read_calendars(monkeypatch, start, end):
    def unexpected(*args):
        pytest.fail("invalid range reached calendar reader")
    monkeypatch.setattr(calendar_sources, "events", unexpected)
    with pytest.raises(ValueError):
        calendar_access.query(start, end)
