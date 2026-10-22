import datetime as dt

import pytest

from agent_media_core import config
from agent_media_server import booking_calendar as bookings, calendar_write


def feed(status="CONFIRMED"):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Rye Remedial//Bookings//EN\r\n"
            "METHOD:PUBLISH\r\nBEGIN:VEVENT\r\nUID:rye-booking-18@ryeremedial.com.au\r\n"
            "DTSTART:20991023T080000Z\r\nDTEND:20991023T090000Z\r\n"
            f"SUMMARY:Massage therapy: Example\r\nSTATUS:{status}\r\n"
            "LOCATION:Shared address\r\nDESCRIPTION:Private contact details\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n").encode()


def test_pending_booking_preserves_uid_status_and_address_without_contact_details():
    event = bookings.parse_feed(feed("TENTATIVE"))[0]
    _, body = calendar_write._body(event)
    assert b"UID:rye-booking-18@ryeremedial.com.au" in body
    assert b"STATUS:TENTATIVE" in body
    assert b"LOCATION:Shared address" in body
    assert b"X-SASONICA-SOURCE:rye-booking" in body
    assert b"Private contact details" not in body


@pytest.mark.parametrize("body", [feed()[:-20], feed().replace(b"PUBLISH", b"REQUEST"),
                                 feed().replace(b"CONFIRMED", b"CANCELLED"),
                                 feed().replace(b"rye-booking-18", b"unrelated")])
def test_incomplete_or_unexpected_feed_rejected(body):
    with pytest.raises(ValueError):
        bookings.parse_feed(body)


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(config, "load", lambda: {"calendar": {"rye": {
        "write_pair": "test-pair", "feed_url": "https://example.invalid/private"}}})
    monkeypatch.setattr(bookings, "_read", lambda url: bookings.parse_feed(feed()))
    monkeypatch.setattr(bookings, "_sync_local", lambda pair: None)
    monkeypatch.setattr(bookings, "_local_owned", lambda pair: [])
    return monkeypatch


def test_create_retry_and_conditional_time_update(adapter):
    writes = []
    response = {"created": True}
    def write(event, **kwargs):
        writes.append((event, kwargs))
        return response
    adapter.setattr(calendar_write, "write", write)
    assert bookings.sync()["changed"] == 1
    response = {"exists": True, "matches": True, "etag": "old"}
    assert bookings.sync()["changed"] == 0
    response = {"exists": True, "matches": False, "etag": "old"}
    assert bookings.sync()["changed"] == 1
    assert writes[-1][1]["etag"] == "old"
    assert all(kwargs["calendar"] == "test-pair" and not kwargs["sync"] for _, kwargs in writes)


def test_failed_source_never_changes_or_deletes_google_events(adapter):
    def failed(url):
        raise RuntimeError("Feed unavailable")
    adapter.setattr(bookings, "_read", failed)
    adapter.setattr(bookings, "_sync_local", lambda pair: pytest.fail("Must read source first"))
    adapter.setattr(calendar_write, "write", lambda *a, **kw: pytest.fail("Must not write"))
    with pytest.raises(RuntimeError):
        bookings.sync()


def test_complete_empty_feed_cancels_only_previously_owned_future_bookings(adapter):
    adapter.setattr(bookings, "_read", lambda url: [])
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    adapter.setattr(bookings, "_local_owned", lambda pair: [
        {"uid": "rye-booking-18@ryeremedial.com.au", "start": future},
        {"uid": "rye-booking-17@ryeremedial.com.au", "start": future - dt.timedelta(days=2)}])
    adapter.setattr(calendar_write, "inspect_event", lambda uid, **kw: {"exists": True, "etag": "tag"})
    writes = []
    adapter.setattr(calendar_write, "write", lambda event, **kw: writes.append((event, kw)))
    assert bookings.sync()["changed"] == 1
    assert writes == [({"uid": "rye-booking-18@ryeremedial.com.au"},
                       {"calendar": "test-pair", "etag": "tag", "delete": True, "sync": False})]


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
