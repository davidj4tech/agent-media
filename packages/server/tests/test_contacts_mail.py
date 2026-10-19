"""A card's "Their emails" (§6.25 `mail`): a mail search over every address a
person has, from the book's own template. Nothing is fetched; HOME, the
config dir and the state dir are throwaway."""

from __future__ import annotations

import json
import os
import urllib.parse

import pytest

from agent_media_server import contacts


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("MATRIX_", "MEDIA_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    contacts._reset_for_tests()
    yield
    contacts._reset_for_tests()


def _query(url: str) -> str:
    return urllib.parse.unquote(url.split("#search/", 1)[1])


def test_gmail_search_over_every_address():
    got = contacts.mail_link([{"value": "lionel@example.com", "type": "home"},
                              {"value": "L.Smith@Work.example"}, {"value": "LIONEL@example.com"}])
    assert got["label"] == "Gmail"
    assert got["url"].startswith("https://mail.google.com/mail/u/0/#search/")
    assert _query(got["url"]) == ("from:(lionel@example.com) OR to:(lionel@example.com) OR "
                                  "from:(L.Smith@Work.example) OR to:(L.Smith@Work.example)")
    assert " " not in got["url"] and "(" not in got["url"]       # encoded whole


def test_no_address_no_link():
    assert contacts.mail_link([]) is None
    assert contacts.mail_link([{"value": ""}, {"value": "not an address"}]) is None


def test_the_owners_template_and_off(monkeypatch):
    monkeypatch.setenv("MEDIA_CONTACTS_MAIL_URL", "https://mail.google.com/mail/u/1/#search/{query}")
    assert "/mail/u/1/#search/" in contacts.mail_link(["a@b.example"])["url"]
    monkeypatch.setenv("MEDIA_CONTACTS_MAIL_URL", "https://webmail.example/search?q={query}")
    got = contacts.mail_link(["a@b.example"])
    assert got == {"url": "https://webmail.example/search?q=" + urllib.parse.quote(
        "from:(a@b.example) OR to:(a@b.example)", safe=""), "label": "webmail.example"}
    monkeypatch.setenv("MEDIA_CONTACTS_MAIL_URL", "off")
    assert contacts.mail_link(["a@b.example"]) is None
    monkeypatch.setenv("MEDIA_CONTACTS_MAIL_URL", "https://nowhere.example/")   # no {query}
    assert contacts.mail_link(["a@b.example"]) is None


def test_another_books_own_setting(monkeypatch):
    key = "acct-" + "1" * 20
    monkeypatch.setenv("MEDIA_CONTACTS_MAIL_URL", "off")             # the owner's, not theirs
    with contacts.as_account(key):
        d = contacts.account_config_dir()
        d.mkdir(parents=True, exist_ok=True)
        assert "mail.google.com" in contacts.mail_link(["a@b.example"])["url"]
        (d / "contacts.json").write_text(json.dumps(
            {"mail_url": "https://mail.google.com/mail/u/2/#search/{query}"}))
        assert "/mail/u/2/" in contacts.mail_link(["a@b.example"])["url"]
        (d / "contacts.json").write_text(json.dumps({"mail_url": "off"}))
        assert contacts.mail_link(["a@b.example"]) is None


def test_on_the_card(monkeypatch):
    conn = contacts._connect()
    cid = contacts.contact_id("android", "L1")
    card = {"name": "Lionel Smith", "names": [{"display": "Lionel Smith"}], "phones": [],
            "emails": [{"value": "lionel@example.com"}], "sources": []}
    conn.execute("INSERT INTO contacts (id, name, card, updated) VALUES (?, ?, ?, ?)",
                 (cid, "Lionel Smith", json.dumps(card), 1.0))
    conn.commit()
    conn.close()
    got = contacts.card(cid)
    assert got["mail"]["label"] == "Gmail" and "lionel%40example.com" in got["mail"]["url"]


# --- old chat gateway addresses (made-up addresses only) ----------------------

GW = [
    "jo.bloggs%hotmail.example@msn2.jabber.dk", "jo%yahoo.example@yahoo.jabber.bluendo.com",
    "12345678@icq.gajim.org", "jo%aol.example@aim.jaim.at", "jo.b@fb.jabbim.cz",
    "jo%gmail.example@j2j.example.net", "jo@gtalk2voip.com", "jo@chikkatalk.com",
    "jo@bot.talk.google.com", "jo%live.example@msn.newtransport.example",
]
REAL = ["jo@yahoo.com.au", "jo@msn.com", "jo@hotmail.example", "jo@jabbim.cz",
        "jo@talk.google.com.example", "jo@yahoo.co.uk"]


def test_gateways_recognised():
    assert all(contacts.is_gateway(a) for a in GW), [a for a in GW if not contacts.is_gateway(a)]
    assert not any(contacts.is_gateway(a) for a in REAL), [a for a in REAL if contacts.is_gateway(a)]


def test_the_real_address_decoded():
    assert contacts.gateway_real("jo.bloggs%hotmail.example@msn2.jabber.dk") == "jo.bloggs@hotmail.example"
    assert contacts.gateway_real("jo%40live.example@msn.gajim.org") == "jo@live.example"
    assert contacts.gateway_real("12345678@icq.gajim.org") is None
    assert contacts.gateway_real("jo@gtalk2voip.com") is None


def test_the_patterns_are_configurable(monkeypatch):
    monkeypatch.setenv("MEDIA_CONTACTS_GATEWAYS", "+*.oldchat.example")
    assert contacts.is_gateway("jo@msn.oldchat.example") and contacts.is_gateway(GW[0])
    monkeypatch.setenv("MEDIA_CONTACTS_GATEWAYS", "*.oldchat.example")
    assert contacts.is_gateway("jo@x.oldchat.example") and not contacts.is_gateway("jo@chikkatalk.com")


def _rec(conn, kind, sid, card, meta=None):
    conn.execute("INSERT OR REPLACE INTO records (kind, sid, card, meta) VALUES (?, ?, ?, ?)",
                 (kind, sid, json.dumps(card), json.dumps(meta or {})))


def _book(records):
    conn = contacts._connect()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM records")
    for r in records:
        _rec(conn, *r)
    contacts._rebuild(conn)
    conn.execute("COMMIT")
    conn.close()
    contacts._fresh()


def _person(name, emails, phones=()):
    return {"name": name, "names": [{"display": name}],
            "emails": [{"value": e} for e in emails],
            "phones": [{"value": p} for p in phones]}


def test_folded_on_the_card_and_kept_after_deletion():
    jo = contacts.contact_id("android", "J1")
    _book([("android", "J1", _person("Jo Bloggs", [
        "jo.bloggs@hotmail.example", "jo.bloggs%hotmail.example@msn2.jabber.dk",
        "jo%yahoo.example@yahoo.jabber.bluendo.com", "12345678@icq.gajim.org"]))])
    c = contacts.card(jo)
    assert [e["value"] for e in c["emails"]] == ["jo.bloggs@hotmail.example", "jo@yahoo.example"]
    assert c["emails"][1]["derived"] is True
    assert [x["address"] for x in c["old_addresses"]] == [
        "jo.bloggs%hotmail.example@msn2.jabber.dk", "jo%yahoo.example@yahoo.jabber.bluendo.com",
        "12345678@icq.gajim.org"]
    assert c["old_addresses"][2]["real"] is None
    q = urllib.parse.unquote(c["mail"]["url"])
    assert "from:(jo@yahoo.example)" in q and "to:(12345678@icq.gajim.org)" in q
    assert "from:(jo.bloggs%hotmail.example@msn2.jabber.dk)" in q
    # Deleted from Google: the card keeps them, and so does the mail search.
    _book([("android", "J1", _person("Jo Bloggs", ["jo.bloggs@hotmail.example"]))])
    c = contacts.card(jo)
    assert [e["value"] for e in c["emails"]] == ["jo.bloggs@hotmail.example"]
    assert len(c["old_addresses"]) == 3
    assert "msn2.jabber.dk" in urllib.parse.unquote(c["mail"]["url"])
    # Kept through a schema bump, like the merges.
    assert "aliases" in contacts._KEPT


def test_a_decoded_address_joins_contacts():
    _book([("android", "J1", _person("Jo Bloggs", ["jo%live.example@msn.gajim.org"])),
           ("google", "people/c1", _person("Jo B", ["jo@live.example"]), {"etag": "e"})])
    rows = contacts.listing()["contacts"]
    assert len(rows) == 1 and rows[0]["emails"] == ["jo@live.example"]
    # The gateway form alone never matches anyone.
    assert contacts._keys(_person("x", ["12345678@icq.gajim.org"]))[1] == set()


def test_a_long_search_is_capped_newest_first():
    many = [f"person{i:03d}%hotmail.example@msn.gajim.org" for i in range(80)]
    got = contacts.mail_link(["jo@real.example"], many)
    assert len(got["url"]) <= contacts.MAIL_URL_MAX and got["left_out"] > 0
    q = urllib.parse.unquote(got["url"])
    assert q.split("#search/")[1].startswith("from:(jo@real.example)")
    assert "person000%hotmail" in q and "person079%hotmail" not in q
    assert contacts.mail_link(["jo@real.example"], many[:2]).get("left_out") is None


def test_older_cards_are_made_again():
    _book([("android", "J1", _person("Jo Bloggs", ["jo%live.example@msn.gajim.org"]))])
    assert contacts.refold() is False                        # already this version
    conn = contacts._connect()
    contacts._set_meta(conn, "cards", "old")
    conn.close()
    assert contacts.refold() is True
    assert contacts.card(contacts.contact_id("android", "J1"))["emails"][0]["value"] == "jo@live.example"
