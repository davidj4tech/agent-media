"""The alert store (alerts.py, server-contract.md §6.17).

The state machine is driven with an explicit clock; the inbox record against a
throwaway inbox.org; the routes over real HTTP to an in-process canvas.
"""

from __future__ import annotations

import pytest

from agent_media_server import alerts

from test_contract import AUTH, call, server, signed_in, typed  # noqa: F401


def rep(now, aid="disk.red5.root", level="ok", **kw):
    ok, d = alerts.report({"id": aid, "level": level, **kw}, now=now)
    assert ok, d
    return d


# --- the state machine -----------------------------------------------------------

def test_ok_is_quiet_and_a_raise_notifies():
    assert rep(1, level="ok")["change"] is None
    d = rep(2, level="warn", title="red5 root is 91% full", step=90)
    assert (d["change"], d["notify"]) == ("raised", True)
    assert d["alert"]["open"] and d["alert"]["first_seen"] == 2


def test_the_same_level_again_only_bumps_last_seen():
    rep(1, level="warn", title="t", step=90)
    d = rep(2, level="warn", step=90)
    assert (d["change"], d["notify"]) == (None, False)
    assert d["alert"]["last_seen"] == 2 and d["alert"]["title"] == "t"


def test_a_higher_step_escalates_and_a_lower_one_rearms():
    rep(1, level="warn", step=90)
    assert rep(2, level="warn", step=95)["change"] == "escalated"
    assert rep(3, level="warn", step=90)["change"] is None
    assert rep(4, level="warn", step=95)["change"] == "escalated"


def test_warn_to_needs_escalates_and_back_eases_quietly():
    rep(1, level="warn")
    assert rep(2, level="needs")["change"] == "escalated"
    d = rep(3, level="warn")
    assert (d["change"], d["notify"]) == ("eased", False)
    assert d["alert"]["peak"] == "needs"


def test_clear_is_quiet_and_closes_the_row():
    rep(1, level="warn")
    d = rep(2, level="ok", title="red5 root is fine")
    assert (d["change"], d["notify"]) == ("cleared", False)
    assert not d["alert"]["open"] and d["alert"]["cleared_at"] == 2


def test_confirm_holds_a_raise_until_it_repeats():
    rep(1, "host.red3", "ok")
    assert rep(2, "host.red3", "warn", confirm=2)["change"] is None
    assert rep(3, "host.red3", "warn", confirm=2)["change"] == "raised"


def test_confirm_resets_when_the_run_is_broken():
    rep(1, "host.red3", "warn", confirm=2)
    rep(2, "host.red3", "ok", confirm=2)
    assert rep(3, "host.red3", "warn", confirm=2)["change"] is None
    assert rep(4, "host.red3", "warn", confirm=2)["change"] == "raised"


def test_a_clear_is_never_held():
    rep(1, "host.red3", "warn")
    assert rep(2, "host.red3", "ok", confirm=5)["change"] == "cleared"


def test_ack_stops_nothing_but_is_forgotten_on_the_next_raise():
    rep(1, level="warn")
    ok, d = alerts.ack("disk.red5.root", now=2)
    assert ok and d["alert"]["acked_at"] == 2
    rep(3, level="ok")
    assert rep(4, level="warn")["alert"]["acked_at"] is None


def test_ack_of_nothing_is_404():
    assert alerts.ack("nope")[1]["status"] == 404


def test_a_spoken_digest_is_rendered_held_and_played_by_its_row(monkeypatch):
    held = []
    monkeypatch.setattr(alerts, "_render_held", lambda *a: held.append(a))
    d = rep(1, "digest.agenda", "info", kind="digest", title="Org agenda",
            detail="a", spoken="Org agenda for today. 3 items.")
    assert len(held) == 1
    aid, title, spoken, key = held[0]
    assert (aid, title, spoken) == ("digest.agenda", "Org agenda",
                                    "Org agenda for today. 3 items.")
    # Rendering: no row yet, so nothing to play.
    assert d["alert"]["speech"] == {"id": None, "heard": False}

    from agent_media_core.state import StateStore
    rid = StateStore().add_history(sink="speech", uri="/tmp/x.mp3", started_at=1,
                                   ended_at=1, target="sasonica", source="cli",
                                   text=spoken, extras={"held": True, "dedup_key": key})
    listed = alerts.spoken_digests(now=2)
    assert [(r["id"], r["speech"]) for r in listed] == [
        ("digest.agenda", {"id": rid, "heard": False})]
    StateStore().mark_heard(rid)
    assert alerts.spoken_digests(now=2)[0]["speech"]["heard"] is True

    # The next digest without a read-out drops the old one's Play.
    rep(3, "digest.agenda", "info", kind="digest", detail="b")
    assert alerts.spoken_digests(now=4) == [] and len(held) == 1


def test_spoken_is_ignored_on_a_status_alert(monkeypatch):
    held = []
    monkeypatch.setattr(alerts, "_render_held", lambda *a: held.append(a))
    d = rep(1, level="warn", spoken="red5 is full")
    assert held == [] and d["alert"]["speech"] is None


def test_digest_is_kept_latest_and_notifies_only_at_warn():
    d = rep(1, "digest.describe", "info", kind="digest", title="TTS 24h", detail="a")
    assert (d["change"], d["notify"]) == ("digest", False)
    d = rep(2, "digest.describe", "warn", kind="digest", detail="b")
    assert d["notify"] and d["alert"]["detail"] == "b"
    assert not d["alert"]["open"]


@pytest.mark.parametrize("body", [{"id": "Bad Id", "level": "ok"},
                                  {"id": "x", "level": "loud"},
                                  {"id": "x", "level": "ok", "kind": "event"}])
def test_bad_reports_are_400(body):
    ok, d = alerts.report(body)
    assert not ok and d["status"] == 400


def test_a_silent_producer_raises_its_own_alert_and_clears_when_heard():
    rep(0, "memory.health", "ok", every_s=3600)
    listing = alerts.listing(now=3 * 3600 + 1)
    silent = [a for a in listing["alerts"] if a["id"] == "memory.health.silent"]
    assert silent and silent[0]["level"] == "warn" and silent[0]["open"]
    rep(3 * 3600 + 2, "memory.health", "ok", every_s=3600)
    after = {a["id"]: a for a in alerts.listing(now=3 * 3600 + 3)["alerts"]}
    assert not after["memory.health.silent"]["open"]


def test_listing_puts_open_first_worst_first():
    rep(1, "a", "warn")
    rep(2, "b", "needs")
    rep(3, "c", "warn")
    rep(4, "c", "ok")
    ids = [a["id"] for a in alerts.listing(now=5)["alerts"]]
    assert ids[:2] == ["b", "a"] and "c" in ids[2:]
    assert [a["id"] for a in alerts.listing(open_only=True, now=5)["alerts"]] == ["b", "a"]


# --- the inbox record --------------------------------------------------------------

@pytest.fixture()
def inbox(tmp_path, monkeypatch):
    p = tmp_path / "inbox.org"
    p.write_text("#+title: Inbox\n\n* TODO Something else\n  body\n")
    monkeypatch.setenv("MEDIA_ALERTS_INBOX", str(p))
    return p


def test_a_raise_files_one_todo_with_the_alert_id(inbox):
    rep(1, level="warn", title="red5 root is 91% full", detail="df line",
        fix="Free space", host="red5")
    rep(2, level="warn", step=95)
    rep(3, level="needs")
    text = inbox.read_text()
    assert text.count(":ALERT_ID: disk.red5.root") == 1
    assert "* TODO red5 root is 91% full" in text and "Fix: Free space" in text
    assert "* TODO Something else" in text


def test_a_routine_clear_closes_the_todo(inbox):
    rep(1, level="warn", title="red5 root is 91% full")
    rep(2, level="ok")
    text = inbox.read_text()
    assert "* DONE red5 root is 91% full\n  CLOSED: [" in text
    assert "Cleared [" in text
    assert text.index("Cleared") < len(text)  # inside the entry
    assert "* TODO Something else" in text


def test_a_needs_or_acked_clear_leaves_it_open(inbox):
    rep(1, "a", "needs", title="login expired")
    rep(2, "a", "ok")
    rep(3, "b", "warn", title="host down")
    alerts.ack("b", now=4)
    rep(5, "b", "ok")
    text = inbox.read_text()
    assert "* TODO login expired" in text and "* TODO host down" in text
    assert text.count("Cleared [") == 2


def test_the_next_incident_files_a_new_todo(inbox):
    rep(1, level="warn", title="full")
    rep(2, level="ok")
    rep(3, level="warn", title="full again")
    text = inbox.read_text()
    assert "* DONE full" in text and "* TODO full again" in text


# --- the routes --------------------------------------------------------------------

def test_report_needs_the_host_token_or_a_device(server, monkeypatch):
    monkeypatch.setenv("AMUX_AUTH_TOKEN", "hosttok")
    body = {"id": "disk.red5.root", "level": "warn", "title": "t"}
    res, _ = call(server, "POST", "/alerts", body)
    assert res.status == 401
    res, _ = call(server, "POST", "/alerts", body, {"Authorization": "Bearer nope"})
    assert res.status == 401
    res, d = call(server, "POST", "/alerts", body, {"X-Auth-Token": "hosttok"})
    assert res.status == 200 and d["change"] == "raised" and d["notify"] is True


def test_list_and_ack_take_the_app_gate(server, signed_in, monkeypatch):
    monkeypatch.setenv("AMUX_AUTH_TOKEN", "hosttok")
    call(server, "POST", "/alerts", {"id": "host.red3", "level": "needs", "title": "red3 down"},
         {"X-Auth-Token": "hosttok"})
    res, d = call(server, "GET", "/alerts?open=1", headers=AUTH)
    assert res.status == 200 and [a["id"] for a in d["alerts"]] == ["host.red3"]
    res, d = call(server, "POST", "/alerts/ack", {"id": "host.red3"}, AUTH)
    assert res.status == 200 and d["alert"]["acked_at"]
    res, _ = call(server, "POST", "/alerts/ack", {"id": "nope"}, AUTH)
    assert res.status == 404


def test_every_digest_is_kept_to_browse_and_read(monkeypatch):
    monkeypatch.setattr(alerts, "_render_held", lambda *a: None)
    body = "## Worth stealing\n\n" + "x" * 10000
    a = rep(1, "digest.landscape", "info", kind="digest", title="Landscape 1", detail=body)
    rep(2, "digest.agenda", "info", kind="digest", title="Agenda", detail="due",
        spoken="Agenda.")
    c = rep(3, "digest.landscape", "info", kind="digest", title="Landscape 2", detail="b")
    # A digest's body is not cut to a status alert's 4,000.
    assert a["alert"]["detail"] == body

    listed = alerts.digests()
    assert [d["title"] for d in listed] == ["Landscape 2", "Agenda", "Landscape 1"]
    assert "detail" not in listed[0]
    assert [d["title"] for d in alerts.digests("digest.landscape")] == [
        "Landscape 2", "Landscape 1"]
    assert [d["title"] for d in alerts.digests(before=listed[1]["n"])] == ["Landscape 1"]

    first, last = listed[2]["n"], listed[0]["n"]
    d = alerts.digest(first)
    assert (d["detail"], d["prev"], d["next"]) == (body, None, last)
    assert alerts.digest(last)["prev"] == first
    assert alerts.digest(9999) is None
    # Home's row opens the latest.
    assert alerts.spoken_digests(now=4)[0]["n"] == listed[1]["n"]
    assert c["alert"]["detail"] == "b"



def test_old_digests_go_after_a_year_but_the_landscape_watch_stays(monkeypatch):
    monkeypatch.setattr(alerts, "_render_held", lambda *a: None)
    rep(1, "digest.landscape", "info", kind="digest", title="Landscape", detail="l")
    rep(2, "digest.agenda", "info", kind="digest", title="Old agenda", detail="a")
    rep(1 + 300 * 86400, "digest.agenda", "info", kind="digest", title="Agenda", detail="b")
    alerts.listing(now=3 + alerts.DIGEST_KEEP_S)
    assert [d["title"] for d in alerts.digests()] == ["Agenda", "Landscape"]

def test_digest_routes(server, signed_in, monkeypatch):  # noqa: F811
    monkeypatch.setattr(alerts, "_render_held", lambda *a: None)
    rep(1, "digest.landscape", "info", kind="digest", title="L", detail="body")
    res, got = call(server, "GET", "/alerts/digests", headers=AUTH)
    assert res.status == 200 and [d["title"] for d in got["digests"]] == ["L"]
    n = got["digests"][0]["n"]
    res, got = call(server, "GET", f"/alerts/digest?n={n}", headers=AUTH)
    assert res.status == 200 and got["digest"]["detail"] == "body"
    res, _ = call(server, "GET", "/alerts/digest?n=999", headers=AUTH)
    assert res.status == 404


def test_a_digest_names_the_view_its_lines_are_items_of(monkeypatch):
    monkeypatch.setattr(alerts, "_render_held", lambda *a: None)
    rep(1, "digest.org-agenda", "info", kind="digest", title="A", detail="x", view="agenda")
    rep(2, "digest.other", "info", kind="digest", title="B", detail="y", view="Not A View!")
    assert [d["view"] for d in alerts.digests()] == [None, "agenda"]
    assert alerts.digest(alerts.digests()[1]["n"])["view"] == "agenda"


# --- notices: what the phone posts ---------------------------------------------------

def test_a_first_connection_gets_only_the_head():
    rep(1, level="warn", title="red5: / at 90%", step=90)
    head = alerts.last_seq()
    assert head > 0
    assert alerts.notices(None, now=2) == {"last": head, "notices": []}
    assert alerts.notices(head, now=2) == {"last": head, "notices": []}


def test_notices_after_the_cursor_are_raises_and_escalations():
    rep(1, level="ok")
    start = alerts.last_seq()
    rep(2, level="warn", title="red5: / at 90%", step=90, detail="df line", fix="clean")
    rep(3, level="warn", step=90, detail="df line", fix="clean")  # same: nothing
    got = alerts.notices(start, now=4)
    assert [(n["change"], n["level"], n["title"], n["detail"], n["fix"])
            for n in got["notices"]] == [("raised", "warn", "red5: / at 90%", "df line", "clean")]
    assert got["last"] == alerts.last_seq()


def test_a_raise_then_an_escalation_is_one_notice():
    start = alerts.last_seq()
    rep(1, level="warn", title="at 90%", step=90)
    rep(2, level="warn", title="at 95%", step=95)
    got = alerts.notices(start, now=3)["notices"]
    assert [(n["change"], n["title"]) for n in got] == [("escalated", "at 95%")]


def test_a_cleared_alert_is_no_notice_and_old_ones_are_not_news():
    start = alerts.last_seq()
    rep(1, level="warn", title="t", step=90)
    rep(2, level="ok")
    assert alerts.notices(start, now=3)["notices"] == []
    rep(10, aid="host.red3", level="needs", title="red3 down")
    assert alerts.notices(start, now=10 + alerts.NOTICE_MAX_AGE_S + 1)["notices"] == []


def test_a_digest_is_a_notice_only_at_warn():
    start = alerts.last_seq()
    rep(1, aid="digest.a", kind="digest", level="info", title="quiet", detail="x")
    rep(2, aid="digest.b", kind="digest", level="warn", title="loud", detail="y")
    assert [n["id"] for n in alerts.notices(start, now=3)["notices"]] == ["digest.b"]


def test_a_cursor_past_the_head_is_reset_to_it():
    rep(1, level="warn", title="t", step=90)
    head = alerts.last_seq()
    assert alerts.notices(head + 50, now=2) == {"last": head, "notices": []}
