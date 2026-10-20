"""Calendars beside the Org items (calendar_sources.py): a vdir folder and an
ICS link, read from config.toml, their repeats expanded. Made-up calendars
only; the clock's zone is pinned to Melbourne so times read the same
everywhere."""

from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("icalendar")

from agent_media_server import app, auth, calendar_sources, org_calendar  # noqa: E402

TZ = "Australia/Melbourne"


def ics(*events: str) -> str:
    body = "".join(f"BEGIN:VEVENT\r\n{e.strip()}\r\nEND:VEVENT\r\n".replace("\n", "\r\n").replace("\r\r", "\r")
                   for e in events)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\n{body}END:VCALENDAR\r\n"


DENTIST = """UID:dentist
SUMMARY:Dentist
LOCATION:12 High St
DTSTART;TZID=Australia/Melbourne:20261022T140000
DTEND;TZID=Australia/Melbourne:20261022T150000"""

# 9:00 UTC is 20:00 in Melbourne (daylight time).
CALL = """UID:call
SUMMARY:Call with London
DTSTART:20261021T090000Z
DTEND:20261021T093000Z"""

HOLIDAY = """UID:holiday
SUMMARY:Holiday
DTSTART;VALUE=DATE:20261024
DTEND;VALUE=DATE:20261027"""

YOGA = """UID:yoga
SUMMARY:Yoga
DTSTART;TZID=Australia/Melbourne:20261005T180000
DTEND;TZID=Australia/Melbourne:20261005T190000
RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261130T070000Z
EXDATE;TZID=Australia/Melbourne:20261012T180000"""

YOGA_MOVED = """UID:yoga
RECURRENCE-ID;TZID=Australia/Melbourne:20261019T180000
SUMMARY:Yoga (moved)
DTSTART;TZID=Australia/Melbourne:20261020T183000
DTEND;TZID=Australia/Melbourne:20261020T193000"""

BINS = """UID:bins
SUMMARY:Bins out
DTSTART;VALUE=DATE:20261001
RRULE:FREQ=WEEKLY;UNTIL=20261231T000000Z"""

CANCELLED = """UID:gone
SUMMARY:Called off
STATUS:CANCELLED
DTSTART;TZID=Australia/Melbourne:20261021T100000
DTEND;TZID=Australia/Melbourne:20261021T110000"""

LATE = """UID:late
SUMMARY:Night shift
DTSTART;TZID=Australia/Melbourne:20261023T220000
DTEND;TZID=Australia/Melbourne:20261024T060000"""


@pytest.fixture(autouse=True)
def melbourne(monkeypatch):
    monkeypatch.setenv("TZ", TZ)
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MEDIA_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(calendar_sources, "_PARSED", {})
    return tmp_path


def configure(home, text: str):
    (home / "config.toml").write_text(text)


def vdir(home, name: str, *events: str, meta: dict | None = None):
    folder = home / "cals" / name
    folder.mkdir(parents=True, exist_ok=True)
    for i, e in enumerate(events):
        (folder / f"{i}.ics").write_text(ics(e))
    for k, v in (meta or {}).items():
        (folder / k).write_text(v)
    return folder


def week():
    return calendar_sources.events(dt.date(2026, 10, 19), dt.date(2026, 10, 25))


def by_title(items):
    out: dict[str, list] = {}
    for it in items:
        out.setdefault(it["title"], []).append(it)
    return out


def test_a_vdir_folder_of_folders(home):
    vdir(home, "personal", DENTIST, CALL, HOLIDAY, CANCELLED, LATE,
         meta={"displayname": "Personal", "color": "#3a7"})
    vdir(home, "work", YOGA + "\n", YOGA_MOVED, BINS)
    configure(home, f'[[calendar.sources]]\nkind = "vdir"\npath = "{home / "cals"}"\n')
    items, cals = week()
    assert [(c["name"], c["ok"]) for c in cals] == [("Personal", True), ("work", True)]
    assert cals[0]["color"] == "#3a7" and "path" not in cals[0]
    got = by_title(items)
    [d] = got["Dentist"]
    assert (d["date"], d["time"], d["end_time"], d["location"], d["all_day"]) == (
        "2026-10-22", "14:00", "15:00", "12 High St", False)
    assert d["calendar"]["name"] == "Personal" and d["source"] == "calendar" and d["uid"] == "dentist"
    [call] = got["Call with London"]
    assert (call["date"], call["time"]) == ("2026-10-21", "20:00")      # UTC made local
    [hol] = got["Holiday"]
    assert (hol["date"], hol["end_date"], hol["all_day"]) == ("2026-10-24", "2026-10-26", True)
    [late] = got["Night shift"]
    assert (late["date"], late["time"], late["end_date"], late["end_time"]) == (
        "2026-10-23", "22:00", "2026-10-24", "06:00")
    assert "Called off" not in got


def test_repeats_with_an_exception_and_a_moved_one(home):
    vdir(home, "work", YOGA, YOGA_MOVED, BINS)
    configure(home, f'[[calendar.sources]]\nkind = "vdir"\npath = "{home / "cals" / "work"}"\nname = "Work"\n')
    # This week's Monday class was moved to Tuesday 18:30.
    got = by_title(week()[0])
    assert "Yoga" not in got
    assert [(y["date"], y["time"]) for y in got["Yoga (moved)"]] == [("2026-10-20", "18:30")]
    assert [b["date"] for b in got["Bins out"]] == ["2026-10-22"]     # Thursdays, all day
    october = by_title(calendar_sources.events(dt.date(2026, 10, 1), dt.date(2026, 10, 31))[0])
    # 5th yes, 12th excluded, 19th moved, 26th yes.
    assert [y["date"] for y in october["Yoga"]] == ["2026-10-05", "2026-10-26"]
    december = by_title(calendar_sources.events(dt.date(2026, 12, 1), dt.date(2026, 12, 31))[0])
    assert "Yoga" not in december and len(december["Bins out"]) == 5


def test_a_bad_file_costs_only_itself(home):
    folder = vdir(home, "personal", DENTIST)
    (folder / "broken.ics").write_text("BEGIN:VCALENDAR\nthis is not\n")
    configure(home, f'[[calendar.sources]]\nkind = "vdir"\npath = "{folder}"\n')
    assert [i["title"] for i in week()[0]] == ["Dentist"]


def test_no_sources_means_no_calendars(home):
    configure(home, "")
    assert week() == ([], [])
    configure(home, f'[[calendar.sources]]\nkind = "vdir"\npath = "{home / "nowhere"}"\n')
    assert week()[1][0]["ok"] is False


class _Feed(BaseHTTPRequestHandler):
    body = b""
    hits = 0

    def do_GET(self):
        type(self).hits += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/calendar")
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *a):
        pass


@pytest.fixture()
def feed():
    _Feed.body = ics(DENTIST).encode()
    _Feed.hits = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Feed)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/secret-abc/basic.ics"
    srv.shutdown()


def test_an_ics_link_is_fetched_kept_and_never_shown(home, feed):
    configure(home, f'[[calendar.sources]]\nkind = "ics"\nurl = "{feed}"\nname = "Family"\n')
    items, cals = week()
    assert [i["title"] for i in items] == ["Dentist"] and _Feed.hits == 1
    assert "secret-abc" not in json.dumps(cals) and cals[0]["ok"] is True
    kept = list((home / "state" / "agent-media" / "calendar").glob("*.ics"))
    assert len(kept) == 1 and oct(os.stat(kept[0]).st_mode & 0o777) == "0o600"
    week()
    assert _Feed.hits == 1                     # fresh: not fetched again
    # Stale and the link failing: the last good copy stands.
    past = time.time() - 3600
    os.utime(kept[0], (past, past))
    _Feed.body = b"<html>gone</html>"
    assert [i["title"] for i in week()[0]] == ["Dentist"]
    for _ in range(50):
        if _Feed.hits == 2:
            break
        time.sleep(0.05)
    assert _Feed.hits == 2


# --- through the route ------------------------------------------------------------------

@pytest.fixture()
def server(home, monkeypatch):
    monkeypatch.setenv("MEDIA_ORG_DIR", str(home / "org"))
    (home / "org").mkdir()
    (home / "org" / "work.org").write_text("* TODO Report\n  DEADLINE: <2026-10-22 Thu>\n")
    monkeypatch.setenv("MEDIA_AGENDA_FILES", str(home / "org" / "work.org"))
    monkeypatch.setenv("MEDIA_ORG_PROFILE", "none")
    monkeypatch.setattr(auth, "gate", lambda bearer: (
        ({"username": "david"}, {}) if bearer == "good"
        else (None, {"error": "not allowed", "status": 401})))
    owner = {"is": True}
    monkeypatch.setattr(org_calendar, "_owner", lambda: owner["is"])
    vdir(home, "personal", DENTIST)
    configure(home, f'[[calendar.sources]]\nkind = "vdir"\npath = "{home / "cals" / "personal"}"\n')
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield srv.server_address, owner
    srv.shutdown()
    srv.server_close()


def _get(addr, path):
    conn = http.client.HTTPConnection(*addr, timeout=10)
    conn.request("GET", path, headers={"Authorization": "Bearer good"})
    res = conn.getresponse()
    data = json.loads(res.read() or b"{}")
    conn.close()
    return res.status, data


def test_the_route_merges_events_for_the_owner_only(server):
    addr, owner = server
    status, got = _get(addr, "/calendar?from=2026-10-19&to=2026-10-25")
    assert status == 200
    assert [(i["title"], i["source"]) for i in got["items"]] == [
        ("Report", "org"), ("Dentist", "calendar")]
    assert [c["name"] for c in got["calendars"]] == ["personal"]
    owner["is"] = False
    _, got = _get(addr, "/calendar?from=2026-10-19&to=2026-10-25")
    assert [i["title"] for i in got["items"]] == ["Report"] and got["calendars"] == []
