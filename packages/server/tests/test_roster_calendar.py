import datetime as dt
import json

import pytest

from agent_media_server import roster_calendar as roster


def packet(first, last, rows):
    return {"ok": True, "complete": True, "start": first.isoformat(), "end": last.isoformat(), "shifts": rows}


def test_designated_hours_are_timed_and_near_days_refine_to_free_context():
    first, last = dt.date(2026, 10, 22), dt.date(2026, 11, 5)
    data = packet(first, last, [
        {"id": "1", "start": "2026-10-22T14:30:00", "end": "2026-10-22T17:45:00", "allDay": True},
        {"id": "2", "start": "2026-10-24T08:45:00", "end": "2026-10-24T14:45:00"}])
    events = roster.shift_events(data, first, last, refined=True)
    assert events[0]["start"] == "2026-10-22T14:30:00+11:00"
    assert events[0]["end"] == "2026-10-22T17:45:00+11:00"
    assert events[0]["busy"] is False
    assert events[1]["busy"] is True
    assert all(e["busy"] for e in roster.shift_events(data, first, last, refined=False))


def test_dst_and_overnight_preserve_published_local_finish():
    first, last = dt.date(2026, 10, 3), dt.date(2026, 10, 5)
    events = roster.shift_events(packet(first, last, [
        {"id": "1", "start": "2026-10-03T22:00:00", "end": "2026-10-04T06:00:00"}]), first, last, False)
    assert events[0]["start"].endswith("+10:00")
    assert events[0]["end"] == "2026-10-04T06:00:00+11:00"


@pytest.mark.parametrize("changes", [{"complete": False}, {"end": "2026-10-22"}, {"shifts": None}])
def test_incomplete_source_is_rejected(changes):
    first, last = dt.date(2026, 10, 22), dt.date(2026, 11, 5)
    data = packet(first, last, []) | changes
    with pytest.raises(ValueError):
        roster.shift_events(data, first, last, False)


def fake_setup(tmp_path, monkeypatch, rows, old=(), treatments=True):
    from agent_media_core import config
    first = dt.datetime.now(roster.ZONE).date()
    last = first + dt.timedelta(days=14)
    reader, node = tmp_path / "reader", tmp_path / "node"
    reader.touch(); node.touch()
    monkeypatch.setattr(config, "load", lambda: {"calendar": {"phs": {
        "write_pair": "work", "roster_command": str(reader), "node": str(node)}}})
    def run(command, **kwargs):
        return type("Result", (), {"returncode": 0, "stdout": json.dumps(packet(first, last, rows))})()
    monkeypatch.setattr(roster.subprocess, "run", run)
    monkeypatch.setattr(roster.calendar_access, "query", lambda *a: {"events": list(old)})
    def treatment_sync():
        if not treatments:
            raise RuntimeError("source unavailable")
        return {"ok": True, "treatments": 1}
    monkeypatch.setattr(roster.phs_calendar, "sync", treatment_sync)
    writes, inspected = [], []
    monkeypatch.setattr(roster.calendar_write, "write", lambda event, **kw: writes.append((event, kw)) or {"created": True})
    monkeypatch.setattr(roster.calendar_write, "inspect_event", lambda uid, **kw: inspected.append(uid) or {"exists": True, "etag": '"1"'})
    return first, writes, inspected


def test_treatment_failure_keeps_designated_shift_busy(tmp_path, monkeypatch):
    tomorrow = dt.datetime.now(roster.ZONE).date() + dt.timedelta(days=1)
    _, writes, _ = fake_setup(tmp_path, monkeypatch, [
        {"id": "1", "start": f"{tomorrow}T10:00:00", "end": f"{tomorrow}T18:00:00"}], treatments=False)
    result = roster.sync()
    assert result["ok"] and not result["treatment_sync_ok"]
    assert writes[0][0]["busy"] is True
    assert writes[0][1]["calendar"] == "work"


def test_empty_unpublished_roster_never_removes_previous_shifts(tmp_path, monkeypatch):
    tomorrow = dt.datetime.now(roster.ZONE).date() + dt.timedelta(days=1)
    old = [{"uid": "phs-shift-old@sasonica", "managed": True, "shift_source": "phs-shift",
            "start_at": f"{tomorrow}T10:00:00+11:00"}]
    _, writes, inspected = fake_setup(tmp_path, monkeypatch, [], old)
    with pytest.raises(RuntimeError, match="unexpectedly empty"):
        roster.sync()
    assert not writes and not inspected


def test_fresh_complete_source_only_cancels_own_missing_future_shift(tmp_path, monkeypatch):
    tomorrow = dt.datetime.now(roster.ZONE).date() + dt.timedelta(days=1)
    old = [
        {"uid": "phs-shift-old@sasonica", "managed": True, "shift_source": "phs-shift", "start_at": f"{tomorrow}T09:00:00+11:00"},
        {"uid": "unrelated", "managed": False, "shift_source": "phs-shift"}]
    _, writes, inspected = fake_setup(tmp_path, monkeypatch, [
        {"id": "new", "start": f"{tomorrow}T10:00:00", "end": f"{tomorrow}T18:00:00"}], old)
    assert roster.sync()["ok"]
    assert inspected == ["phs-shift-old@sasonica"]
    assert [event["uid"] for event, kwargs in writes if kwargs.get("delete")] == ["phs-shift-old@sasonica"]
