import datetime as dt

import pytest
from icalendar import Calendar

from agent_media_server import calendar_write as writer


def event(**updates):
    return {"uid": "test@sasonica", "title": "Shift", "start": "2026-10-27T09:45:00+11:00",
            "end": "2026-10-27T13:45:00+11:00", "source": "marlo", **updates}


def test_recurrence_keeps_local_hours_across_dst():
    from dateutil.rrule import rrulestr
    uid, data = writer._body(event(rrule="FREQ=WEEKLY;INTERVAL=2;COUNT=20"))
    ev = Calendar.from_ical(data).walk("VEVENT")[0]
    starts = list(rrulestr(ev["RRULE"].to_ical().decode(), dtstart=ev.decoded("DTSTART")))
    assert {s.hour for s in starts} == {9}
    assert {s.utcoffset() for s in starts} == {dt.timedelta(hours=10), dt.timedelta(hours=11)}


@pytest.mark.parametrize("updates", [{"start": "2026-10-27T09:45:00"},
                                     {"end": "2026-10-27T08:00:00+11:00"},
                                     {"rrule": "nonsense"}])
def test_bad_events_never_connect(monkeypatch, updates):
    monkeypatch.setattr(writer, "_connection", lambda *_: pytest.fail("connected"))
    with pytest.raises(ValueError):
        writer.write(event(**updates))


def test_conditional_create_readback_and_duplicate(monkeypatch):
    store, calls = {}, []
    monkeypatch.setattr(writer, "_connection", lambda *_: ("test", "https://test/", "private"))
    monkeypatch.setattr(writer.subprocess, "run", lambda *a, **kw: type("R", (), {"returncode": 0})())
    def request(url, auth, method="GET", body=None, headers=None):
        calls.append((method, headers))
        if method == "GET":
            return (200, store["data"], '"1"') if store else (404, b"", "")
        assert headers["If-None-Match"] == "*"
        store["data"] = body
        return 201, b"", '"1"'
    monkeypatch.setattr(writer, "_request", request)
    result = writer.write(event())
    assert result["created"] and result["verified"] and result["local_sync_ok"]
    assert writer.write(event())["exists"]
    assert len([c for c in calls if c[0] == "PUT"]) == 1
    with pytest.raises(writer.CalendarWriteError, match="changed"):
        writer.write(event(title="Changed"), etag='"stale"')


def test_unmanaged_event_not_modified(monkeypatch):
    _, data = writer._body(event())
    data = data.replace(b"X-SASONICA-MANAGED:1\r\n", b"")
    monkeypatch.setattr(writer, "_connection", lambda *_: ("test", "https://test/", "private"))
    monkeypatch.setattr(writer, "_request", lambda *a, **kw: (200, data, '"1"'))
    with pytest.raises(writer.CalendarWriteError, match="not managed"):
        writer.write(event(), etag='"1"')
