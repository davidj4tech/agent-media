import datetime as dt
import json
import sys

import pytest

from agent_media_core import config
from agent_media_server import alerts, booking_calendar as bookings, calendar_write


def entry(n=18, start="20991023T080000Z", end="20991023T090000Z", status="CONFIRMED", name="Nicole"):
    return (f"BEGIN:VEVENT\r\nUID:rye-booking-{n}@ryeremedial.com.au\r\n"
            f"DTSTART:{start}\r\nDTEND:{end}\r\n"
            f"SUMMARY:Massage therapy: {name}\r\nSTATUS:{status}\r\n"
            "LOCATION:Shared address\r\nDESCRIPTION:Mobile: 0400 000 000\\nPrivate contact details\r\n"
            "END:VEVENT\r\n")


def feed(*entries):
    entries = entries or (entry(),)
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Rye Remedial//Bookings//EN\r\n"
            "METHOD:PUBLISH\r\n" + "".join(entries) + "END:VCALENDAR\r\n").encode()


UID18 = "rye-booking-18@ryeremedial.com.au"
UID19 = "rye-booking-19@ryeremedial.com.au"


def test_pending_booking_preserves_uid_status_and_address_without_contact_details():
    event = bookings.parse_feed(feed(entry(status="TENTATIVE")))[0]
    _, body = calendar_write._body(event)
    assert b"UID:rye-booking-18@ryeremedial.com.au" in body
    assert b"STATUS:TENTATIVE" in body
    assert b"LOCATION:Shared address" in body
    assert b"X-SASONICA-SOURCE:rye-booking" in body
    assert b"Private contact details" not in body and b"0400" not in body


@pytest.mark.parametrize("body", [feed()[:-20], feed().replace(b"PUBLISH", b"REQUEST")])
def test_incomplete_or_unexpected_feed_rejected(body):
    with pytest.raises(ValueError):
        bookings.read_feed(body)


def test_cancelled_and_unreadable_entries_are_skipped_not_fatal():
    got = bookings.read_feed(feed(entry(18, status="CANCELLED"), entry(19),
                                  entry(20, end="20991023T070000Z"),  # ends before it starts
                                  entry(21).replace("rye-booking-21", "unrelated")))
    assert [e["uid"] for e in got["events"]] == [UID19]
    assert got["cancelled"] == {UID18}
    assert got["held"] == {"rye-booking-20@ryeremedial.com.au"}
    assert got["unknown"] == 1


def test_a_duplicated_booking_is_held_not_trusted():
    got = bookings.read_feed(feed(entry(18), entry(18, start="20991023T100000Z", end="20991023T110000Z")))
    assert got["events"] == [] and got["held"] == {UID18}


# --- the mirror against a fake Google, site and notifier -------------------------------

class Google:
    """A CalDAV calendar holding only the mirror's events, by UID."""

    def __init__(self):
        self.events, self.writes = {}, []

    def _same(self, a, b):
        keys = ("title", "status", "location")
        return (bookings._span(a["start"], a["end"]) == bookings._span(b["start"], b["end"])
                and all(a.get(k) == b.get(k) for k in keys))

    def write(self, event, calendar="", etag="", delete=False, *, sync=True):
        assert calendar == "test-pair" and sync is False
        uid = event["uid"]
        if delete:
            assert etag == "tag-" + uid
            self.writes.append(("delete", uid))
            del self.events[uid]
            return {"deleted": True}
        if uid not in self.events:
            self.events[uid] = dict(event)
            self.writes.append(("create", uid))
            return {"created": True}
        if not etag:
            return {"exists": True, "etag": "tag-" + uid, "matches": self._same(self.events[uid], event)}
        self.events[uid] = dict(event)
        self.writes.append(("update", uid))
        return {"created": False}

    def inspect_event(self, uid, calendar=""):
        if uid not in self.events:
            return {"uid": uid, "exists": False}
        e = self.events[uid]
        return {"uid": uid, "exists": True, "etag": "tag-" + uid, "start": e["start"], "end": e["end"]}

    def david_moves(self, uid, start, end):
        self.events[uid].update(start=start, end=end)


class Site:
    def __init__(self):
        self.calls = []

    def move(self, booking, *, was, start, end, email):
        self.calls.append(("move", booking, was, start, end, email))

    def cancel(self, booking, *, was):
        self.calls.append(("cancel", booking, was))


def ts(text):
    return int(dt.datetime.fromisoformat(text).timestamp())


@pytest.fixture
def world(monkeypatch, tmp_path):
    for k in list(__import__("os").environ):
        if k.startswith(("MEDIA_", "RELAY_")):
            monkeypatch.delenv(k, raising=False)
    for k in ("HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(k, str(tmp_path / k.lower()))
    rye = {"write_pair": "test-pair", "feed_url": "https://example.invalid/private",
           "site_command": ["fake-site"]}
    monkeypatch.setattr(config, "load", lambda: {"calendar": {"rye": rye}})
    w = type("World", (), {})()
    w.rye, w.google, w.site, w.notices = rye, Google(), Site(), []
    w.feed = feed()
    w.local = []
    monkeypatch.setattr(bookings, "_read", lambda url: bookings.read_feed(w.feed))
    monkeypatch.setattr(bookings, "_sync_local", lambda pair: None)
    monkeypatch.setattr(bookings, "_local_owned", lambda pair: w.local)
    monkeypatch.setattr(bookings, "_site", lambda: w.site)
    monkeypatch.setattr(bookings, "_notify", w.notices.append)
    monkeypatch.setattr(calendar_write, "write", w.google.write)
    monkeypatch.setattr(calendar_write, "inspect_event", w.google.inspect_event)
    w.asks = lambda: [n for n in w.notices if n["id"].startswith(bookings.ASK) and n["level"] == "warn"]
    w.told = lambda: [n for n in w.notices if n["id"].startswith(bookings.TOLD) and n["level"] == "warn"]
    w.settled = lambda: [n for n in w.notices if n["id"].startswith(bookings.ASK) and n["level"] == "ok"]
    return w


def test_unchanged_booking_is_written_once_and_never_asked_about(world):
    assert bookings.sync()["changed"] == 1
    assert bookings.sync()["changed"] == 0
    assert world.google.writes == [("create", UID18)]
    assert world.asks() == [] and world.told() == []
    state = json.loads(bookings._state_path().read_text())
    assert state["bookings"][UID18]["who"] == "Nicole"
    assert "Shared address" not in json.dumps(state) and "0400" not in json.dumps(state)
    assert bookings._state_path().stat().st_mode & 0o077 == 0


def test_website_move_is_followed_in_google(world):
    bookings.sync()
    world.feed = feed(entry(start="20991023T083000Z", end="20991023T093000Z"))
    assert bookings.sync()["changed"] == 1
    assert world.google.events[UID18]["start"] == "2099-10-23T08:30:00+00:00"
    assert world.asks() == []


def test_google_move_is_kept_and_asked_about_once(world):
    bookings.sync()
    world.google.david_moves(UID18, "2099-10-23T08:30:00+00:00", "2099-10-23T09:30:00+00:00")
    for _ in range(3):  # three ten-minute runs
        assert bookings.sync()["waiting"] == 1
    assert world.google.events[UID18]["start"] == "2099-10-23T08:30:00+00:00"
    assert world.google.writes == [("create", UID18)]
    [ask] = world.asks()
    assert ask["id"] == "calendar.rye.booking.18"
    assert ask["title"] == ("You moved Nicole to 7:30–8:30pm Fri 23 Oct in Google. "
                            "Change the website and email Nicole?")
    assert [a["id"] for a in ask["actions"]] == ["apply", "apply-quiet", "keep"]
    assert [a["label"] for a in ask["actions"]][:2] == ["Change and email Nicole", "Change, no email"]
    assert "Shared address" not in json.dumps(ask) and "0400" not in json.dumps(ask)
    # Moved again: asked again, with the new time.
    world.google.david_moves(UID18, "2099-10-23T09:00:00+00:00", "2099-10-23T10:00:00+00:00")
    bookings.sync()
    assert len(world.asks()) == 2 and "8–9pm" in world.asks()[-1]["title"]


def _moved_and_applied(world, start, end, decision="apply"):
    """Booking 18 (8–9am UTC) moved in Google to start/end, then answered."""
    bookings.sync()
    world.google.david_moves(UID18, start, end)
    bookings.sync()
    ask = world.asks()[-1]
    result = bookings.decide("18", decision)
    return ask, result, world.site.calls[-1][-1]


def test_exactly_15_minutes_changes_the_website_without_an_email(world):
    ask, result, email = _moved_and_applied(world, "2099-10-23T08:15:00+00:00", "2099-10-23T09:15:00+00:00")
    assert ask["title"].endswith("Change the booking on the website?")
    assert [a["id"] for a in ask["actions"]] == ["apply", "keep"]
    assert email is False and result["emailed_client"] is False


def test_16_minutes_emails_the_client(world):
    ask, result, email = _moved_and_applied(world, "2099-10-23T08:16:00+00:00", "2099-10-23T09:16:00+00:00")
    assert ask["title"].endswith("Change the website and email Nicole?")
    assert email is True and result["emailed_client"] is True


def test_another_day_emails_the_client(world):
    ask, _, email = _moved_and_applied(world, "2099-10-24T08:00:00+00:00", "2099-10-24T09:00:00+00:00")
    assert "Sat 24 Oct" in ask["title"] and email is True


def test_a_resize_whose_end_moves_20_minutes_emails_the_client(world):
    ask, _, email = _moved_and_applied(world, "2099-10-23T08:00:00+00:00", "2099-10-23T09:20:00+00:00")
    assert ask["title"].endswith("email Nicole?") and email is True


def test_change_no_email_skips_it_once(world):
    _, result, email = _moved_and_applied(world, "2099-10-23T09:00:00+00:00", "2099-10-23T10:00:00+00:00",
                                          "apply-quiet")
    assert email is False and result["decision"] == "apply-quiet"


def test_the_email_rule():
    was = ["2099-10-23T08:00:00+00:00", "2099-10-23T09:00:00+00:00"]
    assert not bookings.emails_client(was, ["2099-10-23T07:45:00+00:00", "2099-10-23T08:45:00+00:00"])
    assert not bookings.emails_client(was, ["2099-10-23T08:00:00+00:00", "2099-10-23T09:15:00+00:00"])
    assert bookings.emails_client(was, ["2099-10-23T08:00:00+00:00", "2099-10-23T09:16:00+00:00"])
    # 10 minutes, but past midnight in Melbourne (13:00Z is midnight AEDT): another day.
    late = ["2099-10-23T12:50:00+00:00", "2099-10-23T13:50:00+00:00"]
    assert bookings.emails_client(late, ["2099-10-23T13:00:00+00:00", "2099-10-23T14:00:00+00:00"])


def test_without_a_site_command_only_keep_is_offered(world):
    del world.rye["site_command"]
    bookings.sync()
    world.google.david_moves(UID18, "2099-10-23T08:30:00+00:00", "2099-10-23T09:30:00+00:00")
    bookings.sync()
    assert [a["id"] for a in world.asks()[0]["actions"]] == ["keep"]


def test_apply_changes_the_website_and_settles_the_ask(world):
    bookings.sync()
    world.google.david_moves(UID18, "2099-10-23T08:30:00+00:00", "2099-10-23T10:00:00+00:00")
    bookings.sync()
    assert bookings.decide("18", "apply")["emailed_client"] is True
    assert world.site.calls == [("move", 18, ts("2099-10-23T08:00:00+00:00"),
                                 ts("2099-10-23T08:30:00+00:00"), ts("2099-10-23T10:00:00+00:00"), True)]
    assert world.settled()[-1]["id"] == "calendar.rye.booking.18"
    # The website's feed now says what Google says: nothing to write or ask.
    world.feed = feed(entry(start="20991023T083000Z", end="20991023T100000Z"))
    asks = len(world.asks())
    assert bookings.sync()["changed"] == 0 and bookings.sync()["waiting"] == 0
    assert len(world.asks()) == asks and world.told() == []
    with pytest.raises(ValueError):
        bookings.decide("18", "apply")  # nothing waiting any more


def test_decline_puts_the_website_time_back_in_google(world):
    bookings.sync()
    world.google.david_moves(UID18, "2099-10-23T08:30:00+00:00", "2099-10-23T09:30:00+00:00")
    bookings.sync()
    result = bookings.decide("rye-booking-18", "keep")
    assert result["mirror"]["waiting"] == 0
    assert world.google.events[UID18]["start"] == "2099-10-23T08:00:00+00:00"
    assert world.site.calls == [] and world.settled()
    bookings.sync()
    assert len(world.asks()) == 1


def test_both_changed_the_website_wins_and_david_is_told(world):
    bookings.sync()
    world.google.david_moves(UID18, "2099-10-23T08:30:00+00:00", "2099-10-23T09:30:00+00:00")
    bookings.sync()
    world.feed = feed(entry(start="20991023T100000Z", end="20991023T110000Z"))
    bookings.sync()
    assert world.google.events[UID18]["start"] == "2099-10-23T10:00:00+00:00"
    [told] = world.told()
    assert "website" in told["title"] and "Nicole" in told["title"]
    assert world.settled() and world.site.calls == []


def test_google_delete_asks_and_never_touches_the_website(world):
    bookings.sync()
    del world.google.events[UID18]
    for _ in range(3):
        bookings.sync()
    assert UID18 not in world.google.events  # left deleted, not recreated
    [ask] = world.asks()
    assert ask["title"].startswith("You deleted Nicole's booking (7–8pm Fri 23 Oct) in Google.")
    assert [a["id"] for a in ask["actions"]] == ["cancel", "keep"]
    assert world.site.calls == []
    bookings.decide("18", "keep")
    assert world.google.events[UID18]["start"] == "2099-10-23T08:00:00+00:00"


def test_google_delete_confirmed_cancels_on_the_website(world):
    bookings.sync()
    del world.google.events[UID18]
    bookings.sync()
    with pytest.raises(ValueError):
        bookings.decide("18", "apply")  # a delete is answered by cancel, not apply
    bookings.decide("18", "cancel")
    assert world.site.calls == [("cancel", 18, ts("2099-10-23T08:00:00+00:00"))]
    world.feed = feed(entry(status="CANCELLED"))
    bookings.sync()
    assert UID18 not in world.google.events


def test_cancelled_in_feed_removes_its_google_copy_and_the_rest_continue(world):
    world.feed = feed(entry(18), entry(19, start="20991023T090000Z", end="20991023T100000Z", name="William"))
    bookings.sync()
    world.feed = feed(entry(18, status="CANCELLED"),
                      entry(19, start="20991023T093000Z", end="20991023T103000Z", name="William"))
    result = bookings.sync()
    assert UID18 not in world.google.events
    assert world.google.events[UID19]["start"] == "2099-10-23T09:30:00+00:00"
    assert result["cancelled"] == 1 and result["errors"] == 0


def test_one_failing_booking_never_stops_the_rest(world, monkeypatch):
    world.feed = feed(entry(18), entry(19, name="William", start="20991023T090000Z", end="20991023T100000Z"))
    real = world.google.write

    def flaky(event, **kw):
        if event["uid"] == UID18:
            raise calendar_write.CalendarWriteError("Calendar request failed (HTTP 500)")
        return real(event, **kw)
    monkeypatch.setattr(calendar_write, "write", flaky)
    result = bookings.sync()
    assert result["errors"] == 1 and UID19 in world.google.events
    health = [n for n in world.notices if n["id"] == bookings.HEALTH][-1]
    assert health["level"] == "warn" and "Nicole" not in json.dumps(health)


def test_unreadable_entry_keeps_its_google_copy_and_stops_no_removal(world):
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    world.feed = feed(entry(18, end="20991023T070000Z"), entry(19, name="William"))
    world.google.events[UID18] = {"uid": UID18, "start": "x", "end": "y"}
    world.local = [{"uid": UID18, "start": future}]
    assert bookings.sync()["skipped"] == 1
    assert UID18 in world.google.events


def test_an_unattributable_entry_turns_off_removal_of_absent_bookings(world):
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    world.google.events[UID19] = {"uid": UID19, "start": "x", "end": "y"}
    world.local = [{"uid": UID19, "start": future}]
    world.feed = feed(entry(18), entry(20).replace("rye-booking-20", "garbled"))
    bookings.sync()
    assert UID19 in world.google.events
    world.feed = feed(entry(18))
    bookings.sync()
    assert UID19 not in world.google.events


def test_failed_source_never_changes_or_deletes_google_events(world, monkeypatch):
    def failed(url):
        raise RuntimeError("Feed unavailable")
    monkeypatch.setattr(bookings, "_read", failed)
    monkeypatch.setattr(bookings, "_sync_local", lambda pair: pytest.fail("Must read source first"))
    monkeypatch.setattr(calendar_write, "write", lambda *a, **kw: pytest.fail("Must not write"))
    with pytest.raises(RuntimeError):
        bookings.sync()


def test_complete_feed_cancels_only_previously_owned_future_bookings(world):
    world.feed = feed()
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    world.google.events[UID19] = {"uid": UID19, "start": "x", "end": "y"}
    world.google.events["rye-booking-17@ryeremedial.com.au"] = {"start": "x", "end": "y"}
    world.local = [{"uid": UID19, "start": future},
                   {"uid": "rye-booking-17@ryeremedial.com.au", "start": future - dt.timedelta(days=2)}]
    bookings.sync()
    assert ("delete", UID19) in world.google.writes
    assert "rye-booking-17@ryeremedial.com.au" in world.google.events


def test_alert_action_routes_to_decide(world):
    bookings.sync()
    world.google.david_moves(UID18, "2099-10-23T08:30:00+00:00", "2099-10-23T09:30:00+00:00")
    bookings.sync()
    ok, detail = bookings.alert_action("calendar.rye.booking.18", "apply")
    assert ok and detail["decision"] == "apply" and world.site.calls
    ok, detail = bookings.alert_action("calendar.rye.booking.18", "apply")
    assert not ok and "Nothing is waiting" in detail["error"]


def test_local_scan_restricts_ownership_to_adapter_and_target_pair(monkeypatch, tmp_path):
    folder = tmp_path / "calendar"
    folder.mkdir()
    event = bookings.parse_feed(feed())[0]
    _, body = calendar_write._body(event)
    (folder / "owned.ics").write_bytes(body)
    (folder / "unowned.ics").write_bytes(feed())
    (folder / "other.ics").write_bytes(body.replace(b"X-SASONICA-SOURCE:rye-booking", b"X-SASONICA-SOURCE:phs-shift"))
    cfg = tmp_path / "vdirsyncer"
    cfg.write_text(f'[pair test-pair]\na = "local"\n[storage local]\ntype = "filesystem"\npath = "{folder}"\n')
    monkeypatch.setattr(config, "load", lambda: {"calendar": {"vdirsyncer_config": str(cfg)}})
    assert [e["uid"] for e in bookings._local_owned("test-pair")] == ["rye-booking-18@ryeremedial.com.au"]


# --- the website seam ------------------------------------------------------------

def _fake_drush(tmp_path, answer):
    script = tmp_path / "drush.py"
    seen = tmp_path / "stdin.php"
    script.write_text("import sys, pathlib\n"
                      f"pathlib.Path({str(seen)!r}).write_text(sys.stdin.read())\n"
                      f"print({json.dumps(answer)!r})\n")
    return [sys.executable, "-I", str(script)], seen


def test_drush_site_sends_only_ids_and_times(tmp_path):
    command, seen = _fake_drush(tmp_path, {"ok": True})
    bookings.DrushSite(command).move(18, was=100, start=200, end=4000, email=True)
    php = seen.read_text()
    assert "rye_booking" in php and "condition('start', $b['start'])" in php
    # The site's own change email, only when asked for and the client has an address.
    assert "if ($n && $in['email'] && $b['email'] !== '')" in php
    # The 'rescheduled' event (a move agreed with the client), not the Move form's 'moved'.
    assert "rye_booking_notify($fields + ['was' => (int) $b['start']] + $b, 'rescheduled')" in php
    assert "'moved')" not in php and "moved_from" not in php
    payload = php.split("base64_decode('")[1].split("'")[0]
    import base64
    assert json.loads(base64.b64decode(payload)) == {"op": "move", "id": 18, "was": 100,
                                                     "start": 200, "end": 4000, "email": True}


@pytest.mark.parametrize("answer,words", [({"ok": False, "error": "changed"}, "changed on the website"),
                                          ({"ok": False, "error": "overlap"}, "overlaps"),
                                          ({"nonsense": 1}, "refused")])
def test_drush_site_refusals_are_errors(tmp_path, answer, words):
    command, _ = _fake_drush(tmp_path, answer)
    with pytest.raises(bookings.SiteError, match=words):
        bookings.DrushSite(command).cancel(18, was=100)


def test_unconfigured_site_says_so(monkeypatch):
    monkeypatch.setattr(config, "load", lambda: {"calendar": {"rye": {}}})
    with pytest.raises(bookings.SiteError, match="not set up"):
        bookings._site()


# --- alert actions (alerts.py) ---------------------------------------------------

def test_alert_actions_are_kept_while_open_and_dispatched(monkeypatch):
    seen = []
    monkeypatch.setitem(alerts.ACTION_HANDLERS, "test.act.", "agent_media_server.booking_calendar:alert_action")
    monkeypatch.setattr(bookings, "alert_action", lambda aid, action: (seen.append((aid, action)), (True, {"done": 1}))[1])
    ok, d = alerts.report({"id": "test.act.1", "level": "warn", "title": "t",
                           "actions": [{"id": "apply", "label": "Do it"}, {"id": "Bad!", "label": "x"},
                                       {"id": "keep", "label": "No"}]}, now=1)
    assert [a["id"] for a in d["alert"]["actions"]] == ["apply", "keep"]
    assert alerts.act("test.act.1", "nope")[1]["status"] == 409
    assert alerts.act("test.act.1", "apply") == (True, {"done": 1}) and seen == [("test.act.1", "apply")]
    assert alerts.act("missing", "apply")[1]["status"] == 404
    alerts.report({"id": "test.act.1", "level": "ok"}, now=2)
    assert alerts.listing(now=3)["alerts"][0]["actions"] == []
    assert alerts.act("test.act.1", "apply")[1]["status"] == 409


def test_an_alert_without_a_handler_is_refused():
    alerts.report({"id": "other.thing", "level": "warn", "title": "t",
                   "actions": [{"id": "apply", "label": "Do it"}]}, now=1)
    assert alerts.act("other.thing", "apply")[1]["status"] == 501
