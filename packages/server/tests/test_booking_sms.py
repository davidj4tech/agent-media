"""Texts to Rye Remedial clients (booking_sms.py) against a fake feed, Google,
site, bridge and clock — never the real bridge, site or phone."""
import datetime as dt
import json
import os
import sys
from zoneinfo import ZoneInfo

import pytest

from agent_media_core import config
from agent_media_server import booking_calendar as bookings, booking_sms as sms, calendar_write

from test_booking_calendar import Google, entry, feed

MEL = ZoneInfo("Australia/Melbourne")
UID18 = "rye-booking-18@ryeremedial.com.au"
UID19 = "rye-booking-19@ryeremedial.com.au"
CLIENT = "0400 111 222"      # a made-up client mobile, only ever from the fake site
TRIAL = "0400 999 888"       # a made-up test_to
# Booking 18: Friday 23 October 2026, 7:00–8:00pm Melbourne (AEDT, +11).
FRI = ("20261023T080000Z", "20261023T090000Z")


def at(text):
    """A Melbourne wall time as the clock's epoch."""
    return dt.datetime.fromisoformat(text).replace(tzinfo=MEL).timestamp()


def epoch(utc_ics):
    return int(dt.datetime.strptime(utc_ics, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc).timestamp())


class Site:
    """The website: the mirror's changes, and the read-only lookup."""

    def __init__(self):
        self.calls, self.rows = [], {}

    def move(self, booking, *, was, start, end, email):
        self.calls.append(("move", booking, was, start, end, email))
        self.rows.setdefault(booking, {"status": "confirmed", "phone": CLIENT})["start"] = start

    def cancel(self, booking, *, was):
        self.calls.append(("cancel", booking, was))
        self.rows[booking]["status"] = "cancelled"

    def lookup(self, booking):
        self.calls.append(("lookup", booking))
        return dict(self.rows.get(booking) or {"status": "missing", "start": 0, "phone": ""})


class Bridge:
    def __init__(self):
        self.sent, self.fail = [], 0

    def __call__(self, cfg):
        return self

    def send(self, number, body, txn):
        if self.fail:
            self.fail -= 1
            raise sms.SmsError("The Google Messages bridge could not be reached")
        self.sent.append((number, body, txn))
        return "$event"


@pytest.fixture
def world(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("MEDIA_", "RELAY_", "MATRIX_")):
            monkeypatch.delenv(k, raising=False)
    for k in ("HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(k, str(tmp_path / k.lower()))
    rye = {"write_pair": "test-pair", "feed_url": "https://example.invalid/private",
           "site_command": ["fake-site"], "sms": {"enabled": True}}
    monkeypatch.setattr(config, "load", lambda: {"calendar": {"rye": rye}})
    w = type("World", (), {})()
    w.rye, w.google, w.site, w.bridge, w.notices = rye, Google(), Site(), Bridge(), []
    w.feed, w.clock = feed(entry(19, start="20261020T000000Z", end="20261020T010000Z", name="Old")), at("2026-10-20T12:00")
    monkeypatch.setattr(bookings, "_read", lambda url: bookings.read_feed(w.feed))
    monkeypatch.setattr(bookings, "_sync_local", lambda pair: None)
    monkeypatch.setattr(bookings, "_local_owned", lambda pair: [])
    monkeypatch.setattr(bookings, "_site", lambda: w.site)
    monkeypatch.setattr(bookings, "_notify", w.notices.append)
    monkeypatch.setattr(bookings, "_clock", lambda: w.clock)
    monkeypatch.setattr(calendar_write, "write", w.google.write)
    monkeypatch.setattr(calendar_write, "inspect_event", w.google.inspect_event)
    monkeypatch.setattr(sms, "_sender", w.bridge)

    def book(n=18, start=FRI[0], end=FRI[1], status="CONFIRMED", name="Nicole Smith", site="confirmed"):
        w.site.rows[n] = {"status": site, "start": epoch(start), "phone": CLIENT}
        return entry(n, start=start, end=end, status=status, name=name)
    w.book = book
    w.texts = lambda: [b for _, b, _ in w.bridge.sent]
    w.alerts = lambda: [x for x in w.notices if x["id"] == sms.ALERT]
    bookings.sync()  # the first pass: what is there already is never texted
    return w


# --- the words -------------------------------------------------------------------

SPAN = ["2026-10-23T08:00:00+00:00", "2026-10-23T09:00:00+00:00"]


def test_the_four_texts():
    assert sms.text("confirmed", "Nicole", SPAN) == (
        "Hi Nicole, your Rye Remedial booking is confirmed for Friday 23 October, 7:00–8:00pm. "
        "Reply here if anything changes.\nDavid")
    assert sms.text("moved", "Nicole", SPAN) == (
        "Hi Nicole, your Rye Remedial booking has moved to Friday 23 October, 7:00–8:00pm. "
        "Reply here if anything changes.\nDavid")
    assert sms.text("reminder", "Nicole", SPAN, "treatment") == (
        "Hi Nicole, a reminder of your treatment tomorrow, Friday 23 October at 7:00pm. "
        "Reply here if anything changes.\nDavid")
    assert sms.text("cancelled", "Nicole", SPAN) == (
        "Hi Nicole, your Rye Remedial booking on Friday 23 October, 7:00–8:00pm has been cancelled. "
        "Reply here to book another time.\nDavid")
    across_noon = ["2026-10-23T00:30:00+00:00", "2026-10-23T01:30:00+00:00"]
    assert "11:30am–12:30pm" in sms.text("moved", "Nicole", across_noon)


def test_first_name_and_service_from_the_title():
    assert sms._first("Massage therapy: nicole smith") == "Nicole"
    assert sms._what("Yoga: Sam") == "yoga session" and sms._what("Massage therapy: Sam") == "treatment"


@pytest.mark.parametrize("raw,want", [("0400 111 222", "+61400111222"), ("+61 400 111 222", "+61400111222"),
                                      ("61400111222", "+61400111222"), ("400111222", "+61400111222"),
                                      ("0061 400 111 222", "+61400111222")])
def test_numbers_become_e164(raw, want):
    assert sms.e164(raw) == want


@pytest.mark.parametrize("raw", ["", "12345", "03 5985 0000x"])
def test_untextable_numbers_say_so_without_the_number(raw):
    with pytest.raises(sms.SmsError) as e:
        sms.e164(raw)
    assert "5985" not in str(e.value)


# --- off unless enabled ----------------------------------------------------------

def test_disabled_by_default(world):
    world.rye["sms"] = {}
    world.feed = feed(world.book())
    bookings.sync()
    world.clock = at("2026-10-22T12:00")  # the reminder window too
    bookings.sync()
    assert world.bridge.sent == [] and not any(c[0] == "lookup" for c in world.site.calls)


def test_texts_found_while_disabled_are_not_sent_when_enabled_later(world):
    world.rye["sms"] = {"enabled": False}
    world.feed = feed(world.book())
    bookings.sync()
    world.rye["sms"] = {"enabled": True}
    bookings.sync()
    assert world.texts() == []


def test_the_first_pass_texts_nothing_already_there(monkeypatch, world, tmp_path):
    bookings._state_path().unlink()
    world.feed = feed(world.book())
    bookings.sync()
    assert world.bridge.sent == []


# --- each kind -------------------------------------------------------------------

def test_a_new_confirmed_booking_is_texted_once(world):
    world.feed = feed(world.book())
    result = bookings.sync()
    bookings.sync()
    assert world.bridge.sent[0][0] == "+61400111222"
    assert world.texts() == [sms.text("confirmed", "Nicole", SPAN)]
    assert result["texts"]["sent"] == 1


def test_a_pending_booking_confirmed_is_texted_then(world):
    world.feed = feed(world.book(status="TENTATIVE", site="pending"))
    bookings.sync()
    assert world.texts() == []
    world.feed = feed(world.book())
    bookings.sync()
    assert world.texts() == [sms.text("confirmed", "Nicole", SPAN)]


def _confirmed(world):
    world.feed = feed(world.book())
    bookings.sync()
    world.bridge.sent.clear()


def test_a_website_move_of_15_minutes_is_not_texted_16_is(world):
    _confirmed(world)
    world.feed = feed(world.book(start="20261023T081500Z", end="20261023T091500Z"))
    bookings.sync()
    assert world.texts() == []
    world.feed = feed(world.book(start="20261023T083100Z", end="20261023T093100Z"))
    bookings.sync()
    assert world.texts() == [sms.text("moved", "Nicole", ["2026-10-23T08:31:00+00:00",
                                                          "2026-10-23T09:31:00+00:00"])]


def test_a_website_move_to_another_day_is_texted(world):
    _confirmed(world)
    world.feed = feed(world.book(start="20261024T080000Z", end="20261024T090000Z"))
    bookings.sync()
    assert ["Saturday 24 October" in t for t in world.texts()] == [True]


def _google_move(world, start, end, decision):
    _confirmed(world)
    world.google.david_moves(UID18, start, end)
    bookings.sync()
    bookings.decide("18", decision)
    # The website's feed then says what Google says: no second text.
    s, e = (dt.datetime.fromisoformat(x).astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            for x in (start, end))
    world.feed = feed(entry(18, start=s, end=e, name="Nicole Smith"))
    bookings.sync()
    return world.texts()


def test_a_google_move_applied_past_15_minutes_is_texted_once(world):
    got = _google_move(world, "2026-10-23T09:00:00+00:00", "2026-10-23T10:00:00+00:00", "apply")
    assert got == [sms.text("moved", "Nicole", ["2026-10-23T09:00:00+00:00", "2026-10-23T10:00:00+00:00"])]


def test_a_google_move_applied_quietly_or_within_15_minutes_is_not_texted(world):
    assert _google_move(world, "2026-10-23T09:00:00+00:00", "2026-10-23T10:00:00+00:00", "apply-quiet") == []


def test_a_google_move_of_15_minutes_is_not_texted(world):
    assert _google_move(world, "2026-10-23T08:15:00+00:00", "2026-10-23T09:15:00+00:00", "apply") == []


def test_a_cancelled_booking_is_texted(world):
    _confirmed(world)
    world.site.rows[18]["status"] = "cancelled"
    # 18 is gone from a complete feed.
    world.feed = feed(entry(19, start="20261020T000000Z", end="20261020T010000Z", name="Old"))
    bookings.sync()
    bookings.sync()
    assert world.texts() == [sms.text("cancelled", "Nicole", SPAN)]


def test_a_declined_or_deleted_booking_is_not_texted_as_cancelled(world):
    _confirmed(world)
    world.site.rows[18]["status"] = "declined"
    world.feed = feed(entry(19, start="20261020T000000Z", end="20261020T010000Z", name="Old"))
    bookings.sync()
    assert world.texts() == []


def test_an_unreadable_feed_entry_stops_cancellation_texts(world):
    _confirmed(world)
    world.site.rows[18]["status"] = "cancelled"
    world.feed = feed(entry(20).replace("rye-booking-20", "garbled"))
    bookings.sync()
    assert world.texts() == []


def test_a_google_delete_cancelled_on_the_website_is_texted_once(world):
    _confirmed(world)
    del world.google.events[UID18]
    bookings.sync()
    bookings.decide("18", "cancel")
    world.feed = feed(entry(19, start="20261020T000000Z", end="20261020T010000Z", name="Old"))
    bookings.sync()
    assert world.texts() == [sms.text("cancelled", "Nicole", SPAN)]


def test_a_text_the_booking_no_longer_fits_is_dropped(world):
    world.feed = feed(world.book())
    world.site.rows[18]["start"] += 3600  # moved on the website since the feed was read
    bookings.sync()
    assert world.texts() == [] and world.alerts() == []
    bookings.sync()
    assert world.texts() == []


# --- the reminder ----------------------------------------------------------------

def test_the_reminder_goes_once_the_day_before_in_its_window(world):
    _confirmed(world)
    for when in ("2026-10-21T12:00", "2026-10-22T09:59", "2026-10-22T18:00"):
        world.clock = at(when)
        bookings.sync()
    assert world.texts() == []
    world.clock = at("2026-10-22T10:00")
    bookings.sync()
    world.clock = at("2026-10-22T15:00")
    bookings.sync()
    world.clock = at("2026-10-23T11:00")
    bookings.sync()
    assert world.texts() == [sms.text("reminder", "Nicole", SPAN, "treatment")]


def test_no_reminder_for_a_pending_booking(world):
    world.feed = feed(world.book(status="TENTATIVE", site="pending"))
    bookings.sync()
    world.clock = at("2026-10-22T12:00")
    bookings.sync()
    assert world.texts() == []


def test_no_reminder_the_same_day_as_its_confirmation(world):
    world.clock = at("2026-10-22T11:00")
    world.feed = feed(world.book())
    bookings.sync()
    world.clock = at("2026-10-22T11:10")
    bookings.sync()
    assert world.texts() == [sms.text("confirmed", "Nicole", SPAN)]


def test_a_booking_moved_to_another_day_is_reminded_again(world):
    _confirmed(world)
    world.clock = at("2026-10-22T12:00")
    bookings.sync()
    world.clock = at("2026-10-22T20:00")
    world.feed = feed(world.book(start="20261024T080000Z", end="20261024T090000Z"))
    bookings.sync()
    world.clock = at("2026-10-23T12:00")
    bookings.sync()
    bookings.sync()
    sat = ["2026-10-24T08:00:00+00:00", "2026-10-24T09:00:00+00:00"]
    assert world.texts() == [sms.text("reminder", "Nicole", SPAN, "treatment"),
                             sms.text("moved", "Nicole", sat),
                             sms.text("reminder", "Nicole", sat, "treatment")]


# --- quiet hours, test_to --------------------------------------------------------

def test_quiet_hours_hold_a_text_until_morning(world):
    world.clock = at("2026-10-20T22:30")
    world.feed = feed(world.book())
    result = bookings.sync()
    assert world.texts() == [] and result["texts"]["waiting"] == 1
    world.clock = at("2026-10-21T07:59")
    bookings.sync()
    assert world.texts() == []
    world.clock = at("2026-10-21T08:00")
    bookings.sync()
    assert world.texts() == [sms.text("confirmed", "Nicole", SPAN)]


def test_a_held_text_is_dropped_once_the_visit_has_started(world):
    world.rye["sms"] = {"enabled": True, "quiet_hours": "00:00-23:59"}
    world.feed = feed(world.book())
    bookings.sync()
    world.clock = at("2026-10-23T19:01")
    world.rye["sms"] = {"enabled": True}
    bookings.sync()
    assert world.texts() == []


def test_test_to_sends_only_to_that_number(world):
    world.rye["sms"] = {"enabled": True, "test_to": TRIAL}
    world.feed = feed(world.book())
    bookings.sync()
    assert [n for n, _, _ in world.bridge.sent] == ["+61400999888"]


# --- no number kept; failures ----------------------------------------------------

def test_no_number_is_stored_or_alerted(world):
    world.bridge.fail = 1
    world.feed = feed(world.book())
    bookings.sync()
    bookings.sync()
    kept = bookings._state_path().read_text() + json.dumps(world.notices)
    for digits in ("400111222", "0400 111 222", "111 222"):
        assert digits not in kept
    assert len(world.texts()) == 1


def test_a_failure_alerts_retries_and_never_stops_the_mirror(world):
    world.bridge.fail = 1
    world.feed = feed(world.book())
    result = bookings.sync()
    assert result["ok"] and result["errors"] == 0 and UID18 in world.google.events
    [warn] = world.alerts()
    assert warn["level"] == "warn" and "confirmed for booking 18" in warn["title"]
    assert "tried again" in warn["detail"]
    bookings.sync()
    assert len(world.texts()) == 1 and world.alerts()[-1]["level"] == "ok"


def test_a_text_failing_three_runs_is_given_up_and_said(world):
    world.bridge.fail = 5
    world.feed = feed(world.book())
    for _ in range(4):
        bookings.sync()
    assert "Given up after 3 tries" in world.alerts()[-1]["detail"]
    world.bridge.fail = 0
    bookings.sync()
    assert world.texts() == []


def test_a_crash_in_the_texts_never_stops_the_mirror(world, monkeypatch):
    monkeypatch.setattr(sms, "detect", lambda *a: 1 / 0)
    world.feed = feed(world.book())
    assert bookings.sync()["ok"] and UID18 in world.google.events
    assert world.alerts()[-1]["level"] == "warn"


def _no_site():
    raise bookings.SiteError("Changing the website is not set up")


def test_without_the_site_nothing_is_sent_to_a_client(world, monkeypatch):
    del world.rye["site_command"]
    monkeypatch.setattr(bookings, "_site", _no_site)
    world.feed = feed(world.book())
    bookings.sync()
    assert world.texts() == [] and world.alerts()[-1]["level"] == "warn"


def test_a_trial_by_test_to_works_without_the_site(world, monkeypatch):
    del world.rye["site_command"]
    world.rye["sms"] = {"enabled": True, "test_to": TRIAL}
    monkeypatch.setattr(bookings, "_site", _no_site)
    world.feed = feed(world.book())
    bookings.sync()
    assert [n for n, _, _ in world.bridge.sent] == ["+61400999888"]


# --- the trial, the site lookup, the bridge ---------------------------------------

def test_the_trial_sends_one_made_up_text_to_the_given_number_only():
    bridge = Bridge()
    got = sms.trial(TRIAL, "reminder", now=at("2026-10-22T12:00"), sender=bridge)
    assert got["text"] == sms.text("reminder", "Nicole", SPAN, "treatment")
    assert [n for n, _, _ in bridge.sent] == ["+61400999888"]
    with pytest.raises(ValueError):
        sms.trial(TRIAL, "hello", sender=bridge)


def test_drush_lookup_is_read_only_and_returns_the_fields(tmp_path):
    script, seen = tmp_path / "drush.py", tmp_path / "stdin.php"
    script.write_text("import sys, pathlib\n"
                      f"pathlib.Path({str(seen)!r}).write_text(sys.stdin.read())\n"
                      "print('{\"ok\": true, \"status\": \"confirmed\", \"start\": 5, \"phone\": \"x\"}')\n")
    got = bookings.DrushSite([sys.executable, "-I", str(script)]).lookup(18)
    assert got == {"status": "confirmed", "start": 5, "phone": "x"}
    php = seen.read_text()
    assert "->select('rye_booking'" in php
    assert "update(" not in php and "delete(" not in php and "insert(" not in php


def test_the_bridge_sender_starts_the_chat_joins_and_sends(monkeypatch, tmp_path):
    from agent_media_server import bridges
    env = tmp_path / "matrix.env"
    env.write_text("MATRIX_HOMESERVER=https://hs.invalid\nMATRIX_OWNER_TOKEN=tok\nMATRIX_OWNER_ID=@d:hs.invalid\n")
    for k in list(os.environ):
        if k.startswith("MATRIX_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(bridges, "find", lambda bid: bridges.Bridge(
        id=bid, url="http://bridge.invalid", secret="s3", homeserver="", config=""))
    calls = []

    def http(method, url, token, body, what, timeout=60):
        calls.append((method, url, token, body))
        return {"dm_room_mxid": "!r:hs.invalid"} if "create_dm" in url else {"event_id": "$e"}
    monkeypatch.setattr(sms.GmessagesSender, "_http", staticmethod(http))
    got = sms.GmessagesSender({**sms.DEFAULTS, "matrix_env": str(env)}).send("+61400999888", "Hi", "txn-1")
    assert got == "$e"
    assert calls[0][:3] == ("POST", "http://bridge.invalid/_matrix/provision/v3/create_dm/%2B61400999888"
                                    "?user_id=%40d%3Ahs.invalid", "s3")
    assert calls[1][:3] == ("POST", "https://hs.invalid/_matrix/client/v3/rooms/%21r%3Ahs.invalid/join", "tok")
    assert calls[2] == ("PUT", "https://hs.invalid/_matrix/client/v3/rooms/%21r%3Ahs.invalid/send/"
                               "m.room.message/txn-1", "tok", {"msgtype": "m.text", "body": "Hi"})


def test_the_bridge_sender_without_a_login_says_so(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("MATRIX_"):
            monkeypatch.delenv(k, raising=False)
    with pytest.raises(sms.SmsError, match="matrix-owner-login"):
        sms.GmessagesSender({**sms.DEFAULTS, "matrix_env": str(tmp_path / "none")}).send("+61400999888", "Hi", "t")
