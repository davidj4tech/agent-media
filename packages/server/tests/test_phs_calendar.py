import datetime as dt

import pytest

from agent_media_server.phs_calendar import parse_grid


def test_only_own_times_no_client_details():
    body = '''<option data-staff="1603">Owner</option>
<td class="cell booked staff-cell" data-reference="123" data-staff="1603"><span class="booking-time">2:45pm – 4:15pm</span><p>Private client medical details</p>
<td class="cell booked staff-cell" data-reference="124" data-staff="2"><span class="booking-time">3:00pm – 4:00pm</span>'''
    result = parse_grid(body, dt.date(2026, 10, 22), "1603")
    assert len(result) == 1
    assert result[0]["start"] == "2026-10-22T14:45:00+11:00"
    assert result[0]["end"] == "2026-10-22T16:15:00+11:00"
    assert "Private" not in str(result) and "medical" not in str(result)
    assert parse_grid(body, dt.date(2026, 10, 22), "1603") == result


@pytest.mark.parametrize("body", ['<html>Login</html>', '''<option data-staff="1603">Owner</option>
<td class="cell booked staff-cell" data-reference="123" data-staff="1603"><span class="booking-time">unknown</span>'''])
def test_incomplete_grid_is_not_an_empty_day(body):
    with pytest.raises(ValueError):
        parse_grid(body, dt.date(2026, 10, 22), "1603")


def test_sync_routes_treatments_to_configured_work_calendar(tmp_path, monkeypatch):
    import io
    from agent_media_core import config
    from agent_media_server import phs_calendar as phs
    script = tmp_path / "session"
    script.touch()
    monkeypatch.setattr(config, "load", lambda: {"calendar": {"write_pair": "personal", "phs": {
        "session_command": str(script), "staff_id": "1603", "write_pair": "work"}}})
    commands, writes = [], []
    def run(command, **kwargs):
        commands.append(command)
        if command[0] == str(script):
            from pathlib import Path
            (Path(kwargs["env"]["PHS_DIR"]) / "session").write_text("fixture")
        return type("Result", (), {"returncode": 0})()
    monkeypatch.setattr(phs.subprocess, "run", run)
    monkeypatch.setattr(phs.http.cookiejar.MozillaCookieJar, "load", lambda *a, **kw: None)
    body = b'''<option data-staff="1603">Owner</option><td class="cell booked staff-cell" data-reference="123" data-staff="1603"><span class="booking-time">11:00pm - 11:45pm</span>'''
    class Opener:
        def open(self, *args, **kwargs):
            return io.BytesIO(body)
    monkeypatch.setattr(phs.urllib.request, "build_opener", lambda *args: Opener())
    monkeypatch.setattr(phs.calendar_access, "query", lambda *args: {"events": []})
    def write(event, **kwargs):
        writes.append(kwargs)
        return {"created": True}
    monkeypatch.setattr(phs.calendar_write, "write", write)
    assert phs.sync()["ok"]
    assert writes and all(w["calendar"] == "work" for w in writes)
    assert commands[-1] == ["vdirsyncer", "sync", "work"]
