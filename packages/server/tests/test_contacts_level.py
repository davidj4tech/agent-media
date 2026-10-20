"""A person's speech level (contacts_level.py, §6.25 "Speech level"; David,
20 Oct 2026: one scale for threads and people): kept on the card through a
schema bump, the level of their rooms unless a room has its own, moved from
the first Default/Priority/Quiet setting, and the rooms' old quiet seed
moved to the fallback. HOME and the state dirs are throwaway; no
MEDIA_*/MATRIX_* setting of this host leaks in; nothing is played."""

from __future__ import annotations

import json
import os
import time

import pytest

from agent_media_core import speak_priority
from agent_media_server import contacts, contacts_level, matrix
from test_contract import AUTH, call, server, signed_in, typed  # noqa: F401


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("MATRIX_", "MEDIA_", "RELAY_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", str(tmp_path / "no-such-adb") + " shell")
    monkeypatch.setenv("MEDIA_TIMELINE_NOTMUCH", str(tmp_path / "no-such-notmuch"))
    monkeypatch.setattr(matrix, "_ROOMS", {})
    contacts._reset_for_tests()
    contacts_level._CACHE.clear()
    speak_priority.set_fallback(None)
    yield
    speak_priority.set_fallback(None)
    contacts_level._CACHE.clear()
    contacts._reset_for_tests()


def _google(n, name, emails=()):
    return ("google", f"people/c{n}",
            {"name": name, "names": [{"display": name, "given": name.split()[0],
                                      "family": name.split()[-1]}],
             "phones": [], "emails": [{"value": e, "type": "Home"} for e in emails],
             "addresses": [], "organizations": [], "notes": ""},
            {"etag": f"e{n}"})


def _book(*recs):
    conn = contacts._connect()
    try:
        for kind, sid, card, meta in recs:
            conn.execute("INSERT OR REPLACE INTO records (kind, sid, card, meta) VALUES (?, ?, ?, ?)",
                         (kind, sid, json.dumps(card), json.dumps(meta)))
        contacts._rebuild(conn)
    finally:
        conn.close()
    contacts._INDEX["checked"] = 0.0


LIONEL = contacts.contact_id("google", "people/c1")
ANA = contacts.contact_id("google", "people/c2")


@pytest.fixture
def book():
    _book(_google(1, "Lionel Smith", ["Lionel@Example.com"]), _google(2, "Ana Lee"))


def test_default_until_set_and_back(book):
    assert contacts.card(LIONEL)["level"] is None
    assert contacts_level.set_level(LIONEL, "auto") == "auto"
    assert contacts.card(LIONEL)["level"] == "auto"
    assert contacts.card(ANA)["level"] is None
    assert contacts_level.set_level(LIONEL, "default") is None
    assert contacts.card(LIONEL)["level"] is None


def test_bad_level_or_person(book):
    with pytest.raises(ValueError):
        contacts_level.set_level(LIONEL, "loud")
    with pytest.raises(ValueError):
        contacts_level.set_level(LIONEL, "priority")     # the old word is not a level
    with pytest.raises(KeyError):
        contacts_level.set_level("00000000-0000-0000-0000-000000000000", "quiet")


def test_kept_through_a_schema_bump(book, monkeypatch):
    contacts_level.set_level(LIONEL, "pocket")
    monkeypatch.setattr(contacts, "SCHEMA_VERSION", "test-bump")
    _book(_google(1, "Lionel Smith", ["lionel@example.com"]))
    assert contacts.card(LIONEL)["level"] == "pocket"


def test_the_old_setting_is_moved_once(book):
    conn = contacts._connect()
    try:
        conn.execute("CREATE TABLE notify (contact TEXT NOT NULL, channel TEXT NOT NULL, "
                     "mode TEXT NOT NULL, at REAL, PRIMARY KEY (contact, channel))")
        conn.executemany("INSERT INTO notify VALUES (?, ?, ?, ?)", [
            (LIONEL, "chats", "priority", 1.0), (LIONEL, "email", "quiet", 1.0),
            (ANA, "email", "quiet", 1.0)])
    finally:
        conn.close()
    assert contacts.card(LIONEL)["level"] == "auto"      # chats wins: priority → auto
    assert contacts.card(ANA)["level"] == "quiet"        # no chats row: email's
    conn = contacts._connect()
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "notify" not in names


class _Room:
    def __init__(self, thread):
        self.thread = thread


def _rooms(monkeypatch):
    monkeypatch.setattr(matrix, "_ROOMS", {"t-lionel": _Room("t-lionel"),
                                           "t-stranger": _Room("t-stranger")})
    monkeypatch.setattr(contacts, "for_room",
                        lambda r: {"id": LIONEL, "name": "Lionel Smith"} if r.thread == "t-lionel" else None)
    contacts_level.register()


def test_a_room_takes_its_persons_level_unless_its_own(book, monkeypatch):
    _rooms(monkeypatch)
    assert speak_priority.level_of("t-lionel") == "quiet"         # nobody's level: quiet
    assert speak_priority.source_of("t-lionel") == "room"
    contacts_level.set_level(LIONEL, "auto")
    assert speak_priority.level_of("t-lionel") == "auto"
    assert speak_priority.source_of("t-lionel") == "person"
    assert speak_priority.level_of("t-stranger") == "quiet"
    assert speak_priority.level_of("not-a-room") == speak_priority.default_level()
    speak_priority.set_level("t-lionel", "normal")                # the room's own wins
    assert speak_priority.level_of("t-lionel") == "normal"
    assert speak_priority.source_of("t-lionel") == "own"
    speak_priority.set_level("t-lionel", "auto")                  # its fallback: clears its own
    assert speak_priority.source_of("t-lionel") == "person"


def test_an_address_has_its_persons_level(book):
    contacts_level.set_level(LIONEL, "interrupt")
    assert contacts_level.level_for_address("Lionel@example.com") == "interrupt"
    assert contacts_level.level_for_address("nobody@example.com") is None


def test_another_account_sees_only_the_room_default(book, monkeypatch):
    _rooms(monkeypatch)
    contacts_level.set_level(LIONEL, "auto")
    monkeypatch.setattr(contacts, "is_owner", lambda: False)
    assert contacts_level.fallback("t-lionel") == ("quiet", "room")


def test_the_old_quiet_seed_becomes_the_fallback(book, monkeypatch, tmp_path):
    _rooms(monkeypatch)
    seeded = matrix.matrix.state_dir() / "seeded-quiet.json"
    seeded.parent.mkdir(parents=True, exist_ok=True)
    seeded.write_text(json.dumps(["t-lionel", "t-stranger", "t-kept"]))
    speak_priority.set_level("t-lionel", "quiet", keep=True)      # the old seed's level
    speak_priority.set_level("t-kept", "auto", keep=True)         # one David chose
    contacts_level.set_level(LIONEL, "auto")
    matrix.unseed_quiet()
    assert speak_priority.source_of("t-lionel") == "person"       # the seed gone: the person's
    assert speak_priority.level_of("t-stranger") == speak_priority.default_level()  # set to default then: kept
    assert speak_priority.source_of("t-stranger") == "own"
    assert speak_priority.level_of("t-kept") == "auto"
    assert not seeded.exists()
    matrix.unseed_quiet()                                         # once only


def test_the_rows_say_where_a_level_comes_from(book, monkeypatch):
    from agent_media_server import sessions

    _rooms(monkeypatch)
    contacts_level.set_level(LIONEL, "pocket")
    monkeypatch.setattr(matrix, "rows", lambda flags: [{"session": "t-lionel", "source": "matrix"},
                                                       {"session": "t-stranger", "source": "matrix"}])
    got = {r["session"]: r for r in sessions.sessions_index() if r.get("source") == "matrix"}
    assert (got["t-lionel"]["speech"], got["t-lionel"]["speech_from"]) == ("pocket", "person")
    assert (got["t-stranger"]["speech"], got["t-stranger"]["speech_from"]) == ("quiet", "room")
    assert got["t-lionel"]["speech_own"] is False
    assert got["t-lionel"]["speech_fallback"] == {"level": "pocket", "from": "person"}


def test_the_route(server, signed_in, book):
    res, obj = call(server, "POST", f"/contacts/{LIONEL}/level", {"level": "auto"}, headers=AUTH)
    assert res.status == 200 and obj["level"] == "auto"
    res, obj = call(server, "GET", f"/contacts/{LIONEL}", headers=AUTH)
    assert obj["contact"]["level"] == "auto"
    res, obj = call(server, "POST", f"/contacts/{LIONEL}/level", {"level": "default"}, headers=AUTH)
    assert res.status == 200 and obj["level"] is None
    res, _ = call(server, "POST", f"/contacts/{LIONEL}/level", {"level": "loud"}, headers=AUTH)
    assert res.status == 400
    res, _ = call(server, "POST", "/contacts/00000000-0000-0000-0000-000000000000/level",
                  {"level": "quiet"}, headers=AUTH)
    assert res.status == 404


def test_the_older_notify_form_is_read_as_a_level(server, signed_in, book):
    res, obj = call(server, "POST", f"/contacts/{LIONEL}/notify",
                    {"channel": "chats", "mode": "priority"}, headers=AUTH)
    assert res.status == 200 and obj["level"] == "auto"
    res, obj = call(server, "POST", f"/contacts/{LIONEL}/notify",
                    {"channel": "chats", "mode": "default"}, headers=AUTH)
    assert obj["level"] is None


def test_the_route_is_gated(server, book, monkeypatch):
    from agent_media_server import auth_abs

    monkeypatch.setattr(auth_abs, "abs_urls", lambda: ["http://abs"])
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: (None, 401))
    res, _ = call(server, "POST", f"/contacts/{LIONEL}/level", {"level": "quiet"})
    assert res.status in (401, 403)
    assert contacts.card(LIONEL)["level"] is None
