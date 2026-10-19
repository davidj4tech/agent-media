"""A person's own notification setting (contacts_notify.py, §6.25
"Notifications"): kept on the card per channel, through a schema bump, and
answered to the intake for their rooms and their addresses. HOME and the
state dirs are throwaway; no MEDIA_*/MATRIX_* setting of this host leaks
in; nothing is played."""

from __future__ import annotations

import json
import os

import pytest

from agent_media_core import contact_notify
from agent_media_server import contacts, contacts_notify, matrix
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
    contacts_notify._CACHE.clear()
    contact_notify.set_resolver(None)
    yield
    contact_notify.set_resolver(None)
    contacts_notify._CACHE.clear()
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
    assert contacts.card(LIONEL)["notify"] == {"chats": "default", "email": "default"}
    assert contacts_notify.set_mode(LIONEL, "chats", "priority") == {"chats": "priority",
                                                                      "email": "default"}
    contacts_notify.set_mode(LIONEL, "email", "quiet")
    assert contacts.card(LIONEL)["notify"] == {"chats": "priority", "email": "quiet"}
    assert contacts.card(ANA)["notify"] == {"chats": "default", "email": "default"}
    contacts_notify.set_mode(LIONEL, "chats", "default")
    conn = contacts._connect()
    try:
        assert conn.execute("SELECT channel FROM notify WHERE contact=?", (LIONEL,)).fetchall() == [("email",)]
    finally:
        conn.close()


def test_bad_channel_mode_or_person(book):
    with pytest.raises(ValueError):
        contacts_notify.set_mode(LIONEL, "calls", "quiet")
    with pytest.raises(ValueError):
        contacts_notify.set_mode(LIONEL, "chats", "loud")
    with pytest.raises(KeyError):
        contacts_notify.set_mode("00000000-0000-0000-0000-000000000000", "chats", "quiet")


def test_kept_through_a_schema_bump(book, monkeypatch):
    contacts_notify.set_mode(LIONEL, "chats", "quiet")
    monkeypatch.setattr(contacts, "SCHEMA_VERSION", "test-bump")
    _book(_google(1, "Lionel Smith", ["lionel@example.com"]))
    assert contacts.card(LIONEL)["notify"]["chats"] == "quiet"


class _Room:
    def __init__(self, thread):
        self.thread = thread


def test_the_intake_is_answered_for_rooms_and_addresses(book, monkeypatch):
    contacts_notify.set_mode(LIONEL, "chats", "priority")
    contacts_notify.set_mode(LIONEL, "email", "quiet")
    monkeypatch.setattr(matrix, "_ROOMS", {"!a": _Room("t-lionel"), "!b": _Room("t-stranger")})
    monkeypatch.setattr(contacts, "for_room",
                        lambda r: {"id": LIONEL, "name": "Lionel Smith"} if r.thread == "t-lionel" else None)
    contacts_notify.register()
    assert contact_notify.mode("t-lionel", "chats") == "priority"
    assert contact_notify.mode("t-stranger", "chats") == "default"
    assert contact_notify.mode("no-such-room", "chats") == "default"
    assert contact_notify.mode("lionel@example.com", "email") == "quiet"
    assert contact_notify.mode("nobody@example.com", "email") == "default"


def test_another_account_is_never_asked(book, monkeypatch):
    contacts_notify.set_mode(LIONEL, "email", "quiet")
    monkeypatch.setattr(contacts, "is_owner", lambda: False)
    assert contacts_notify.resolve("lionel@example.com", "email") is None


def test_the_route(server, signed_in, book):
    res, obj = call(server, "POST", f"/contacts/{LIONEL}/notify",
                    {"channel": "chats", "mode": "quiet"}, headers=AUTH)
    assert res.status == 200 and obj["notify"] == {"chats": "quiet", "email": "default"}
    res, obj = call(server, "GET", f"/contacts/{LIONEL}", headers=AUTH)
    assert obj["contact"]["notify"]["chats"] == "quiet"
    res, _ = call(server, "POST", f"/contacts/{LIONEL}/notify",
                  {"channel": "chats", "mode": "loud"}, headers=AUTH)
    assert res.status == 400
    res, _ = call(server, "POST", "/contacts/00000000-0000-0000-0000-000000000000/notify",
                  {"channel": "chats", "mode": "quiet"}, headers=AUTH)
    assert res.status == 404


def test_the_route_is_gated(server, book, monkeypatch):
    from agent_media_server import auth_abs

    monkeypatch.setattr(auth_abs, "abs_urls", lambda: ["http://abs"])
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: (None, 401))
    res, _ = call(server, "POST", f"/contacts/{LIONEL}/notify", {"channel": "chats", "mode": "quiet"})
    assert res.status in (401, 403)
    assert contacts.card(LIONEL)["notify"]["chats"] == "default"
