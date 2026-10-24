import pytest

from zoneinfo import ZoneInfo

from agent_media_server.calendar_publish import snapshot as _snapshot

ZONE = ZoneInfo("Australia/Melbourne")
SHIFTS = {"spa": 75, "roster": 75}
OWN = r"site-booking-\d+@example\.com"


def snapshot(result, now=None, **kw):
    return _snapshot(result, now, **({"zone": ZONE, "shift_sources": SHIFTS, "own_uids": OWN} | kw))


def row(start, end, **kwargs):
    return {"start_at": start, "end_at": end, **kwargs}


def test_free_markers_ignored_and_latest_shift_plus_75_minutes():
    data = {"start": "2026-10-27", "end": "2026-11-01", "sources": [{"ok": True}],
            "events": [row("2026-10-27T00:00:00+11:00", "2026-11-02T00:00:00+11:00", busy=False),
                       row("2026-10-27T09:45:00+11:00", "2026-10-27T13:45:00+11:00",
                           busy=True, managed=True, shift_source="spa", title="private"),
                       row("2026-10-27T15:00:00+11:00", "2026-10-27T17:00:00+11:00",
                           busy=False, managed=True, shift_source="roster")]}
    out = snapshot(data, 1)
    from datetime import datetime
    assert out["open_after"] == {"2026-10-27": int(datetime.fromisoformat("2026-10-27T18:15:00+11:00").timestamp())}
    assert len(out["busy"]) == 1
    assert "private" not in str(out) and "title" not in str(out)


def test_unavailable_source_never_publishes_empty_schedule():
    with pytest.raises(ValueError):
        snapshot({"sources": [{"ok": False}], "events": []})


def test_overnight_shift_holds_both_days_until_75_minutes_after_finish():
    result = snapshot({"start": "2026-10-27", "end": "2026-10-28", "sources": [{"ok": True}],
                       "events": [row("2026-10-27T20:00:00+11:00", "2026-10-28T02:00:00+11:00",
                                      busy=True, managed=True, shift_source="roster")]})
    assert result["open_after"]["2026-10-27"] == result["open_after"]["2026-10-28"]


def test_destination_bookings_are_not_echoed_back_as_external_conflicts():
    start, end = "2026-10-23T19:00:00+11:00", "2026-10-23T20:00:00+11:00"
    data = {"start": "2026-10-23", "end": "2026-10-23", "sources": [{"ok": True}],
            "events": [row(start, end, uid="site-booking-18@example.com", busy=True)]}
    assert snapshot(data)["busy"] == []
    # Only this destination's booking IDs qualify, not unrelated calendar events.
    for uid in ("other@example.com", "site-booking-18@another.example", "site-booking-not-an-id@example.com"):
        data["events"] = [row(start, end, uid=uid, busy=True)]
        assert len(snapshot(data)["busy"]) == 1


def test_unlisted_sources_open_nothing_and_minutes_are_per_source():
    data = {"start": "2026-10-27", "end": "2026-10-27", "sources": [{"ok": True}],
            "events": [row("2026-10-27T09:00:00+11:00", "2026-10-27T10:00:00+11:00",
                           busy=True, managed=True, shift_source="other")]}
    assert snapshot(data)["open_after"] == {}
    out = snapshot(data, shift_sources={"other": 30})
    from datetime import datetime
    assert out["open_after"] == {"2026-10-27": int(datetime.fromisoformat("2026-10-27T10:30:00+11:00").timestamp())}
    # With no own_uids nothing is left out.
    data["events"] = [row("2026-10-27T09:00:00+11:00", "2026-10-27T10:00:00+11:00", uid="site-booking-1@example.com")]
    assert len(snapshot(data, own_uids="")["busy"]) == 1


def test_failed_before_command_does_not_publish_a_new_snapshot(monkeypatch, tmp_path):
    import json
    from agent_media_server import calendar_publish
    file = tmp_path / "publish.json"
    file.write_text(json.dumps({"endpoint": "https://example.invalid/publish", "token": "test",
                                "before": ["~/bin/mirror", "sync"]}))
    file.chmod(0o600)
    runs = []
    def run(argv, **kw):
        runs.append(argv)
        return type("R", (), {"returncode": 1})()
    monkeypatch.setattr(calendar_publish.subprocess, "run", run)
    monkeypatch.setattr(calendar_publish.urllib.request, "build_opener", lambda *a: pytest.fail("Must not publish"))
    with pytest.raises(RuntimeError, match="before command failed"):
        calendar_publish.publish(str(file))
    assert runs == [[__import__("os").path.expanduser("~/bin/mirror"), "sync"]]
