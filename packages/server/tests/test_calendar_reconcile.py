"""media calendar reconcile: one managed source kept in step with a full list."""
import datetime as dt

import pytest

from agent_media_server import calendar_reconcile as rec

NOW = dt.datetime(2026, 10, 25, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=11)))


def event(uid, start, end, **extra):
    return {"uid": uid, "title": "Work", "start": start, "end": end, **extra}


def old(uid, start, source="roster", managed=True):
    return {"uid": uid, "start_at": start, "managed": managed, "shift_source": source}


@pytest.fixture
def calendar(monkeypatch):
    state = {"current": [], "writes": [], "deletes": [], "runs": [], "existing": {}}
    monkeypatch.setattr(rec.calendar_access, "query", lambda *a: {"events": list(state["current"])})
    def write(ev, calendar, etag="", delete=False, sync=True):
        if delete:
            state["deletes"].append(ev["uid"])
            return {"deleted": True}
        state["writes"].append((ev, calendar, etag))
        if ev["uid"] in state["existing"] and not etag:
            return {"exists": True, "matches": state["existing"][ev["uid"]], "etag": '"7"'}
        return {"created": True}
    monkeypatch.setattr(rec.calendar_write, "write", write)
    monkeypatch.setattr(rec.calendar_write, "inspect_event", lambda uid, calendar: {"exists": True, "etag": '"1"'})
    monkeypatch.setattr(rec.subprocess, "run", lambda command, **kw: state["runs"].append(command)
                        or type("R", (), {"returncode": 0})())
    return state


def run(events, **kw):
    return rec.reconcile(events, **({"source": "roster", "calendar": "work", "start": "2026-10-25",
                                      "end": "2026-10-27", "now": NOW} | kw))


def test_writes_new_updates_changed_and_tags_the_source(calendar):
    calendar["existing"] = {"b": False, "c": True}
    result = run([event("a", "2026-10-25T10:00:00+11:00", "2026-10-25T11:00:00+11:00"),
                  event("b", "2026-10-26T10:00:00+11:00", "2026-10-26T11:00:00+11:00"),
                  event("c", "2026-10-26T12:00:00+11:00", "2026-10-26T13:00:00+11:00")])
    assert result == {"ok": True, "source": "roster", "events": 3, "changed": 2, "removed": 0}
    assert all(ev["source"] == "roster" and cal == "work" for ev, cal, _ in calendar["writes"])
    assert [(ev["uid"], etag) for ev, _, etag in calendar["writes"] if etag] == [("b", '"7"')]
    assert calendar["runs"] == [["vdirsyncer", "sync", "work"]]


def test_finished_events_are_left_alone(calendar):
    run([event("gone", "2026-10-25T06:00:00+11:00", "2026-10-25T08:00:00+11:00")])
    assert not calendar["writes"]


def test_removes_only_own_future_entries_inside_the_range(calendar):
    calendar["current"] = [old("mine", "2026-10-26T10:00:00+11:00"),
                           old("started", "2026-10-25T08:00:00+11:00"),
                           old("other", "2026-10-26T10:00:00+11:00", source="marlo"),
                           old("unmanaged", "2026-10-26T10:00:00+11:00", managed=False),
                           old("kept", "2026-10-26T12:00:00+11:00"),
                           old("later", "2026-10-30T10:00:00+11:00")]
    result = run([event("kept", "2026-10-26T12:00:00+11:00", "2026-10-26T13:00:00+11:00")])
    assert calendar["deletes"] == ["mine"]
    assert result["removed"] == 1


def test_refuse_empty_keeps_everything(calendar):
    calendar["current"] = [old("mine", "2026-10-26T10:00:00+11:00")]
    with pytest.raises(RuntimeError):
        run([], refuse_empty=True)
    assert not calendar["deletes"] and not calendar["runs"]
    run([])
    assert calendar["deletes"] == ["mine"]


@pytest.mark.parametrize("events,kw", [
    ([], {"source": "Not OK"}), ([], {"calendar": ""}), ([], {"end": "2026-10-24"}),
    ([{"title": "no uid"}], {}),
    ([event("a", "2026-10-26T10:00:00+11:00", "2026-10-26T11:00:00+11:00", source="marlo")], {})])
def test_bad_input_changes_nothing(calendar, events, kw):
    with pytest.raises(ValueError):
        run(events, **kw)
    assert not calendar["writes"] and not calendar["deletes"]


def test_no_sync(calendar):
    run([], sync=False)
    assert not calendar["runs"]
