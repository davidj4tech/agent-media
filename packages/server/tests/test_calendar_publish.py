import pytest

from agent_media_server.calendar_publish import snapshot


def row(start, end, **kwargs):
    return {"start_at": start, "end_at": end, **kwargs}


def test_free_markers_ignored_and_latest_shift_plus_75_minutes():
    data = {"start": "2026-10-27", "end": "2026-11-01", "sources": [{"ok": True}],
            "events": [row("2026-10-27T00:00:00+11:00", "2026-11-02T00:00:00+11:00", busy=False),
                       row("2026-10-27T09:45:00+11:00", "2026-10-27T13:45:00+11:00",
                           busy=True, managed=True, shift_source="marlo", title="private"),
                       row("2026-10-27T15:00:00+11:00", "2026-10-27T17:00:00+11:00",
                           busy=False, managed=True, shift_source="phs-shift")]}
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
                                      busy=True, managed=True, shift_source="phs-shift")]})
    assert result["open_after"]["2026-10-27"] == result["open_after"]["2026-10-28"]
