"""Every "Same person?" in one place (contacts_review.py, GET
/contacts/suggestions) and the nickname suggestions. Made-up people only;
HOME, the config and state dirs are throwaway, and no MEDIA_* or MATRIX_*
setting of this host leaks in."""

from __future__ import annotations

import json
import os

import pytest

from agent_media_server import contacts, contacts_review, matrix
from test_contract import AUTH, call, server, signed_in, typed  # noqa: F401

SAM = "@sam:ryer.org"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("MATRIX_", "MEDIA_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MATRIX_SAM_ID", SAM)
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", str(tmp_path / "no-such-adb") + " shell")
    monkeypatch.setattr(matrix, "_ROOMS", {})
    contacts._reset_for_tests()
    contacts_review._CACHE.clear()

    def nowhere(*a, **k):
        raise AssertionError(f"a real Google request: {a[:2]}")
    monkeypatch.setattr(contacts, "_request", nowhere)
    yield
    contacts._reset_for_tests()


def _card(name, phones=(), handles=None):
    c = {"name": name, "names": [{"display": name, "given": "", "family": ""}],
         "phones": [{"value": contacts.e164(p, "AU"), "raw": p} for p in phones],
         "emails": [], "addresses": [], "organizations": [], "notes": ""}
    if handles:
        c["handles"] = handles
    return c


def _google(n, name, phones=()):
    return ("google", f"people/c{n}", _card(name, phones), {"etag": f"e{n}"})


def _ghost(uid, name, network="facebookgo", network_name="Messenger", phones=()):
    return ("bridges", uid, _card(name, phones, [{"network": network,
                                                   "network_name": network_name, "id": uid}]),
            {"network": network, "network_name": network_name, "rooms": ["!r:ryer.org"]})


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


G = lambda n: contacts.contact_id("google", f"people/c{n}")  # noqa: E731
B = lambda uid: contacts.contact_id("bridges", uid)  # noqa: E731
DAVE, LIZ, ANA, SOLO = ("@facebook_1:ryer.org", "@facebook_2:ryer.org",
                        "@signal_=2b61400000999:ryer.org", "@facebook_3:ryer.org")


@pytest.fixture
def book():
    _book(_google(1, "David Jones", ["0412 000 001"]),
          _google(2, "Elizabeth Wong"),
          _google(3, "Ana Lee", ["0400 000 111"]),
          _google(4, "Lionel Smith"),
          _ghost(DAVE, "Dave Jones"),
          _ghost(LIZ, "Liz Wong", "instagramgo", "Instagram"),
          _ghost(ANA, "Ana Lee", "signal", "Signal", phones=["0400 000 999"]),
          _ghost(SOLO, "Dave"),
          _ghost("@facebook_4:ryer.org", "Lionel Smith"))


def test_nickname_keys():
    assert contacts_review.nick_keys("dave jones") == {"dave jones", "david jones"}
    assert contacts_review.nick_keys("david jones") == {"david jones"}
    assert contacts_review.nick_keys("dave") == set()          # no surname: nobody


def test_a_nickname_with_the_same_surname_is_a_suggestion(book):
    assert contacts.suggestions(B(DAVE)) == [{"id": G(1), "name": "David Jones", "why": "nickname"}]
    assert contacts.suggestions(G(2)) == [{"id": B(LIZ), "name": "Liz Wong", "why": "nickname"}]
    # never a join: Dave is still his own person
    assert contacts.card(B(DAVE)) is not None


def test_numbers_of_their_own_are_two_people(book):
    assert contacts.suggestions(G(3)) == []
    assert contacts.suggestions(B(ANA)) == []


def test_a_placeholder_name_suggests_no_one(book):
    # Facebook shows every deleted or deactivated account as "Facebook User"
    _book(_google(5, "Facebook User"),
          _ghost("@facebook_5:ryer.org", "Facebook User"),
          _ghost("@facebook_6:ryer.org", "Facebook User"),
          _ghost("@instagram_7:ryer.org", "Instagram User", "instagramgo", "Instagram"))
    assert contacts.suggestions(B("@facebook_5:ryer.org")) == []
    assert contacts.suggestions(G(5)) == []
    assert all("Facebook User" not in (p["a"]["name"], p["b"]["name"])
               for p in contacts_review.pairs()["suggestions"])


def test_every_pair_once(book):
    got = contacts_review.pairs()
    pairs = {(p["a"]["id"], p["b"]["id"], p["why"]) for p in got["suggestions"]}
    assert got["count"] == 3
    assert {frozenset((a, b)) for a, b, _ in pairs} == {
        frozenset((B(DAVE), G(1))), frozenset((B(LIZ), G(2))),
        frozenset((B("@facebook_4:ryer.org"), G(4)))}
    first = got["suggestions"][0]
    assert first["why"] == "same name"                    # same names first
    side = next(p for p in got["suggestions"] if p["why"] == "nickname"
                and B(DAVE) in (p["a"]["id"], p["b"]["id"]))
    who = side["a"] if side["a"]["id"] == B(DAVE) else side["b"]
    assert who["sources"] == ["Messenger"] and who["last"] is None


def test_the_route_and_its_answers(server, signed_in, book):
    res, obj = call(server, "GET", "/contacts/suggestions", headers=AUTH)
    assert res.status == 200 and obj["ok"] and obj["count"] == 3
    res, obj = call(server, "POST", f"/contacts/{B(DAVE)}/different", {"other": G(1)}, headers=AUTH)
    assert res.status == 200
    res, obj = call(server, "POST", f"/contacts/{B(LIZ)}/same", {"other": G(2)}, headers=AUTH)
    assert res.status == 200 and obj["id"] == G(2)
    res, obj = call(server, "GET", "/contacts/suggestions", headers=AUTH)
    assert obj["count"] == 1
    assert obj["suggestions"][0]["why"] == "same name"


def test_the_route_is_gated(server, book, monkeypatch):
    from agent_media_server import auth_abs

    monkeypatch.setattr(auth_abs, "abs_urls", lambda: ["http://abs"])
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: (None, 401))
    res, _ = call(server, "GET", "/contacts/suggestions")
    assert res.status in (401, 403)
