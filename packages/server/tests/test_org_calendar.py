"""The Organiser's calendar (org_calendar.py): Org timestamps with their
times, spans and repeats, for any range of days. Each test builds its own
tree; plain Org, its agenda files set."""

from __future__ import annotations

import datetime as dt
import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from agent_media_server import app, auth, org, org_calendar

TODAY = dt.date(2026, 10, 20)  # a Tuesday

WORK = """\
#+TODO: TODO NEXT | DONE

* TODO Stand-up
  SCHEDULED: <2026-10-19 Mon 09:30 +1d>
* NEXT Report
  DEADLINE: <2026-10-23 Fri>
* TODO Late thing
  SCHEDULED: <2026-10-15 Thu>
* DONE Finished
  SCHEDULED: <2026-10-21 Wed>
* Dentist
  <2026-10-22 Thu 14:00-15:00>
* Conference
  <2026-10-26 Mon>--<2026-10-28 Wed 17:00>
* <2026-10-24 Sat 19:00> Dinner at Mum's
* Rent
  SCHEDULED: <2026-01-31 Sat +1m>
* Old meeting
  <2026-10-01 Thu 10:00>
* Undated
"""


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    root = tmp_path / "org"
    root.mkdir()
    (root / "work.org").write_text(WORK)
    monkeypatch.setenv("MEDIA_ORG_DIR", str(root))
    monkeypatch.setenv("MEDIA_ORG_PROFILE", "none")
    monkeypatch.setenv("MEDIA_AGENDA_FILES", str(root / "work.org"))
    monkeypatch.setattr(auth, "gate", lambda bearer: (
        ({"username": "david"}, {}) if bearer == "good"
        else (None, {"error": "not allowed", "status": 401})))
    return root


def _items(start, end, **kw):
    return org_calendar.items(dt.date.fromisoformat(start), dt.date.fromisoformat(end),
                              today=TODAY, **kw)


def _on(items, title):
    return [h for h in items if h["title"] == title]


def test_a_week_has_times_spans_and_repeats(tree):
    week = _items("2026-10-19", "2026-10-25")
    stand = _on(week, "Stand-up")
    assert [h["date"] for h in stand] == [f"2026-10-{d}" for d in range(19, 26)]
    assert stand[0]["time"] == "09:30" and stand[0]["repeat"] == "+1d"
    assert stand[0]["type"] == "scheduled"
    [dentist] = _on(week, "Dentist")
    assert (dentist["type"], dentist["date"], dentist["time"], dentist["end_time"]) == (
        "event", "2026-10-22", "14:00", "15:00")
    [dinner] = _on(week, "Dinner at Mum's")     # a stamp in the heading itself
    assert (dinner["date"], dinner["time"]) == ("2026-10-24", "19:00")
    [report] = _on(week, "Report")
    assert (report["type"], report["state"]) == ("deadline", "NEXT")
    assert not _on(week, "Finished") and not _on(week, "Undated")
    assert "Finished" in {h["title"] for h in _items("2026-10-19", "2026-10-25", done=True)}
    # Sorted by day, then time.
    assert [(h["date"], h.get("time", "")) for h in week] == sorted(
        (h["date"], h.get("time", "")) for h in week)


def test_overdue_shows_on_its_day_and_on_today(tree):
    late = _on(_items("2026-10-12", "2026-10-25"), "Late thing")
    assert [(h["date"], h["overdue"], h.get("due")) for h in late] == [
        ("2026-10-15", False, None), ("2026-10-20", True, "2026-10-15")]
    # Today outside the range: not carried in.
    assert [h["date"] for h in _on(_items("2026-10-26", "2026-11-01"), "Late thing")] == []
    # An event that has passed is never overdue.
    assert [h["overdue"] for h in _on(_items("2026-09-28", "2026-10-25"), "Old meeting")] == [False]


def test_a_span_shows_where_any_of_its_days_is_in_range(tree):
    [conf] = _on(_items("2026-10-27", "2026-11-02"), "Conference")
    assert (conf["date"], conf["end_date"], conf["end_time"]) == (
        "2026-10-26", "2026-10-28", "17:00")
    assert not _on(_items("2026-10-29", "2026-11-02"), "Conference")


def test_monthly_repeats_keep_their_day(tree):
    rent = _on(_items("2026-01-01", "2026-03-31"), "Rent")
    assert [h["date"] for h in rent] == ["2026-01-31", "2026-02-28", "2026-03-31"]
    # Not marked done since January: overdue on today too, as Org shows it.
    assert [(h["date"], h["overdue"]) for h in _on(_items("2026-10-01", "2026-10-31"), "Rent")] == [
        ("2026-10-20", True), ("2026-10-31", False)]


def test_a_daily_repeat_from_long_ago_skips_to_the_range(tree):
    (tree / "work.org").write_text("* Pills\n  <2000-01-01 Sat 08:00 +1d>\n")
    got = _items("2026-10-19", "2026-10-25")
    assert [h["date"] for h in got] == [f"2026-10-{d}" for d in range(19, 26)]


@pytest.fixture()
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True)
    t.start()
    yield srv.server_address
    srv.shutdown()
    srv.server_close()


def _get(addr, path, bearer="good"):
    conn = http.client.HTTPConnection(*addr, timeout=10)
    conn.request("GET", path, headers={"Authorization": f"Bearer {bearer}"})
    res = conn.getresponse()
    data = json.loads(res.read() or b"{}")
    conn.close()
    return res.status, data


def test_the_route_is_gated_and_checks_the_range(tree, server):
    assert _get(server, "/calendar?from=2026-10-19&to=2026-10-25", bearer="bad")[0] == 401
    status, got = _get(server, "/calendar?from=2026-10-19&to=2026-10-25")
    assert status == 200 and got["from"] == "2026-10-19" and got["to"] == "2026-10-25"
    assert "today" in got and any(h["title"] == "Dentist" for h in got["items"])
    assert _get(server, "/calendar?from=2026-10-25&to=2026-10-19")[0] == 400
    assert _get(server, "/calendar?from=2026-01-01&to=2026-12-31")[0] == 400
    assert _get(server, "/calendar?from=soon")[0] == 400
    _, views = _get(server, "/org")
    assert views["calendar"] is True
