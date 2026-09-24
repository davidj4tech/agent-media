"""Changing notes: a heading's state (with Org's CLOSED and repeaters) and
moving it between the GTD files (notes_edit.py). Each test writes its own
tree; nothing reaches ~/org."""

from __future__ import annotations

import datetime as dt

import pytest

from agent_media_server import auth, notes_edit

INBOX = """\
#+title: Inbox

* THIS WEEK
** TODO Fix the TV ssh
   SCHEDULED: <2026-09-20 Sun 08:45>
   Body about the telly.
*** a sub-point
** NEXT [#A] Call the bank :phone:
** TODO Water plants
   SCHEDULED: <2026-09-20 Sun +1d>
* LATER
** WAITING Parcel
"""

NOW = dt.datetime(2026, 9, 22, 10, 30)


@pytest.fixture(autouse=True)
def _paragtd(monkeypatch):
    """These trees are laid out the paragtd way, and the tests were written
    against its views and refile targets (the notes-paragtd package; plain
    Org is test_notes_plain.py)."""
    monkeypatch.setenv("MEDIA_NOTES_PROFILE", "paragtd")


@pytest.fixture()
def org(tmp_path, monkeypatch):
    root = tmp_path / "org"
    root.mkdir()
    (root / "inbox.org").write_text(INBOX)
    (root / "next-actions.org").write_text("#+title: Next actions\n\n* Inbox\n** NEXT Existing\n\n* Other\n")
    (root / "roam").mkdir()
    (root / "roam" / "n.org").write_text("* TODO roam\n")
    monkeypatch.setenv("MEDIA_NOTES_DIR", str(root))
    monkeypatch.setattr(auth, "gate", lambda b: ({"username": "david"}, {}) if b == "good"
                        else (None, {"error": "no", "status": 401}))
    return root


def _line(title: str) -> int:
    return next(i + 1 for i, ln in enumerate(INBOX.splitlines()) if title in ln)


def test_done_closes_on_the_planning_line_and_reopen_takes_it_off(org):
    ok, got = notes_edit.set_state("inbox.org", _line("Fix the TV"), "Fix the TV ssh", "done", "good", now=NOW)
    assert ok and got == {"path": "inbox.org", "at": 4, "state": "DONE", "repeated": False}
    text = (org / "inbox.org").read_text()
    assert "** DONE Fix the TV ssh\n   CLOSED: [2026-09-22 Tue 10:30] SCHEDULED: <2026-09-20 Sun 08:45>\n" in text
    ok, _ = notes_edit.set_state("inbox.org", 4, "Fix the TV ssh", "TODO", "good", now=NOW)
    assert (org / "inbox.org").read_text() == INBOX


def test_done_without_a_planning_line_adds_one_and_keeps_priority_and_tags(org):
    ok, _ = notes_edit.set_state("inbox.org", _line("Call the bank"), "Call the bank", "DONE", "good", now=NOW)
    text = (org / "inbox.org").read_text()
    assert "** DONE [#A] Call the bank :phone:\n   CLOSED: [2026-09-22 Tue 10:30]\n" in text
    notes_edit.set_state("inbox.org", _line("Call the bank"), "Call the bank", "NEXT", "good", now=NOW)
    assert (org / "inbox.org").read_text() == INBOX


def test_a_repeater_moves_on_instead_of_closing(org):
    ok, got = notes_edit.set_state("inbox.org", _line("Water plants"), "Water plants", "DONE", "good", now=NOW)
    assert ok and got["repeated"] and got["state"] == "TODO" and got["next"] == "2026-09-21"
    assert "** TODO Water plants\n   SCHEDULED: <2026-09-21 Mon +1d>\n" in (org / "inbox.org").read_text()


@pytest.mark.parametrize("stamp,after", [
    ("<2026-07-20 Mon ++1w>", "<2026-09-28 Mon ++1w>"),     # next one after today
    ("<2026-07-20 Mon .+2d>", "<2026-09-24 Thu .+2d>"),     # from today
    ("<2026-01-31 Sat 09:00 +1m>", "<2026-02-28 Sat 09:00 +1m>"),  # month end clamps
    ("<2026-09-20 Sun +1y>", "<2027-09-20 Mon +1y>"),
])
def test_repeater_kinds(stamp, after):
    line, _ = notes_edit._advance(f"   SCHEDULED: {stamp}", NOW.date())
    assert line == f"   SCHEDULED: {after}"


def test_a_moved_line_is_found_by_title_and_a_gone_one_is_409(org):
    ok, got = notes_edit.set_state("inbox.org", 2, "Parcel", "DONE", "good", now=NOW)
    assert ok and got["at"] == _line("Parcel")
    ok, got = notes_edit.set_state("inbox.org", 4, "No such thing", "DONE", "good", now=NOW)
    assert not ok and got["status"] == 409


def test_refile_to_next_actions_moves_the_subtree_under_inbox(org):
    ok, got = notes_edit.refile("inbox.org", _line("Fix the TV"), "Fix the TV ssh", "next", "good")
    assert ok and got["path"] == "next-actions.org"
    inbox = (org / "inbox.org").read_text()
    assert "Fix the TV" not in inbox and "a sub-point" not in inbox and "Call the bank" in inbox
    nxt = (org / "next-actions.org").read_text()
    assert nxt == ("#+title: Next actions\n\n* Inbox\n** NEXT Existing\n"
                   "** NEXT Fix the TV ssh\n   SCHEDULED: <2026-09-20 Sun 08:45>\n"
                   "   Body about the telly.\n*** a sub-point\n\n* Other\n")
    assert nxt.splitlines()[got["at"] - 1] == "** NEXT Fix the TV ssh"


def test_refile_to_the_tickler_needs_a_date_and_schedules_it(org):
    ok, got = notes_edit.refile("inbox.org", _line("Parcel"), "Parcel", "tickler", "good")
    assert not ok and got["status"] == 400
    ok, got = notes_edit.refile("inbox.org", _line("Parcel"), "Parcel", "tickler", "good",
                                date="2026-10-01")
    assert ok
    assert (org / "tickler.org").read_text() == (
        "* Tickler\n** WAITING Parcel\n   SCHEDULED: <2026-10-01 Thu>\n")
    ok, _ = notes_edit.refile("inbox.org", _line("Fix the TV"), "Fix the TV ssh", "tickler",
                              "good", date="2026-10-02")
    tick = (org / "tickler.org").read_text()
    assert "** TODO Fix the TV ssh\n   SCHEDULED: <2026-10-02 Fri> 08:45>" not in tick
    assert "** TODO Fix the TV ssh\n   SCHEDULED: <2026-10-02 Fri>\n" in tick


def test_refile_to_someday_goes_top_level(org):
    ok, _ = notes_edit.refile("inbox.org", _line("Fix the TV"), "Fix the TV ssh", "someday", "good")
    assert (org / "someday.org").read_text().startswith(
        "* TODO Fix the TV ssh\n   SCHEDULED: <2026-09-20 Sun 08:45>\n   Body about the telly.\n** a sub-point\n")


def test_a_new_date_keeps_the_time_and_the_repeater(org):
    ok, got = notes_edit.set_date("inbox.org", _line("Fix the TV"), "Fix the TV ssh",
                                  "scheduled", "2026-09-26", "good")
    assert ok and got == {"path": "inbox.org", "at": 4, "kind": "scheduled",
                          "date": "2026-09-26", "time": "08:45"}
    assert "** TODO Fix the TV ssh\n   SCHEDULED: <2026-09-26 Sat 08:45>\n" in (org / "inbox.org").read_text()
    notes_edit.set_date("inbox.org", 4, "Fix the TV ssh", "scheduled", "2026-09-26", "good", time="10:00")
    assert "SCHEDULED: <2026-09-26 Sat 10:00>\n" in (org / "inbox.org").read_text()
    notes_edit.set_date("inbox.org", 4, "Fix the TV ssh", "scheduled", "2026-09-26", "good", time="")
    assert "SCHEDULED: <2026-09-26 Sat>\n" in (org / "inbox.org").read_text()
    notes_edit.set_date("inbox.org", _line("Water plants"), "Water plants", "scheduled", "2026-10-01", "good")
    assert "** TODO Water plants\n   SCHEDULED: <2026-10-01 Thu +1d>\n" in (org / "inbox.org").read_text()


def test_a_date_is_added_beside_the_other_or_on_a_new_line_and_taken_off(org):
    notes_edit.set_date("inbox.org", 4, "Fix the TV ssh", "deadline", "2026-09-30", "good")
    text = (org / "inbox.org").read_text()
    assert "   SCHEDULED: <2026-09-20 Sun 08:45> DEADLINE: <2026-09-30 Wed>\n" in text
    ok, got = notes_edit.set_date("inbox.org", 0, "Call the bank", "scheduled", "2026-09-23", "good")
    assert ok and "** NEXT [#A] Call the bank :phone:\n   SCHEDULED: <2026-09-23 Wed>\n" in (org / "inbox.org").read_text()
    notes_edit.set_date("inbox.org", got["at"], "Call the bank", "scheduled", "", "good")
    notes_edit.set_date("inbox.org", 4, "Fix the TV ssh", "deadline", "", "good")
    assert (org / "inbox.org").read_text() == INBOX


def test_bad_dates_are_refused(org):
    assert notes_edit.set_date("inbox.org", 4, "", "scheduled", "26/9", "good")[1]["status"] == 400
    assert notes_edit.set_date("inbox.org", 4, "", "closed", "2026-09-26", "good")[1]["status"] == 400
    assert notes_edit.set_date("inbox.org", 4, "", "scheduled", "2026-09-26", "good", time="7pm")[1]["status"] == 400
    assert notes_edit.set_date("inbox.org", 4, "", "scheduled", "2026-09-26", "bad")[1]["status"] == 401
    assert notes_edit.set_date("roam/n.org", 1, "", "scheduled", "2026-09-26", "good")[1]["status"] == 400
    assert (org / "inbox.org").read_text() == INBOX


def test_edits_are_gated_and_kept_to_the_gtd_files(org):
    assert notes_edit.set_state("inbox.org", 4, "", "DONE", "bad")[1]["status"] == 401
    for rel in ("roam/n.org", "../x.org", "astro.org"):
        ok, got = notes_edit.set_state(rel, 1, "", "DONE", "good")
        assert not ok and got["status"] == 400
    assert notes_edit.set_state("inbox.org", 4, "", "MAYBE", "good")[1]["status"] == 400
    assert notes_edit.refile("inbox.org", 4, "", "inbox", "good")[1]["status"] == 400
    assert notes_edit.refile("inbox.org", 4, "", "mars", "good")[1]["status"] == 400
    assert (org / "inbox.org").read_text() == INBOX


def test_the_routes(org):
    import http.client
    import json
    import threading
    from http.server import ThreadingHTTPServer

    from agent_media_server import app

    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def post(path, body):
        c = http.client.HTTPConnection(*srv.server_address, timeout=5)
        c.request("POST", path, json.dumps(body), {"Authorization": "Bearer good"})
        r = c.getresponse()
        return r.status, json.loads(r.read())

    try:
        status, got = post("/notes/state", {"path": "inbox.org", "at": 4,
                                            "title": "Fix the TV ssh", "state": "DONE"})
        assert status == 200 and got["state"] == "DONE"
        status, got = post("/notes/refile", {"path": "inbox.org", "at": _line("Parcel") + 1,
                                             "title": "Parcel", "to": "waiting"})
        assert status == 200 and got["path"] == "waiting-for.org"
        assert (org / "waiting-for.org").read_text() == "* Waiting\n** WAITING Parcel\n"
        status, got = post("/notes/date", {"path": "inbox.org", "at": _line("Call the bank"),
                                           "title": "Call the bank", "date": "2026-09-24",
                                           "time": "19:00"})
        assert status == 200 and got["kind"] == "scheduled" and got["time"] == "19:00"
        assert "SCHEDULED: <2026-09-24 Thu 19:00>" in (org / "inbox.org").read_text()
        assert post("/notes/refile", {"path": "inbox.org", "at": 1, "title": "Gone",
                                      "to": "next"})[0] == 409
    finally:
        srv.shutdown()
        srv.server_close()


def test_priority_is_set_changed_and_taken_off(org):
    at = _line("Call the bank")
    ok, got = notes_edit.set_priority("inbox.org", at, "Call the bank", "b", "good")
    assert ok and got["priority"] == "B"
    assert "** NEXT [#B] Call the bank :phone:" in (org / "inbox.org").read_text()
    ok, _ = notes_edit.set_priority("inbox.org", at, "Call the bank", "", "good")
    assert "** NEXT Call the bank :phone:" in (org / "inbox.org").read_text()
    ok, _ = notes_edit.set_priority("inbox.org", _line("Fix the TV ssh"), "Fix the TV ssh", "A", "good")
    assert "** TODO [#A] Fix the TV ssh" in (org / "inbox.org").read_text()
    ok, got = notes_edit.set_priority("inbox.org", at, "Call the bank", "D", "good")
    assert not ok and got["status"] == 400
