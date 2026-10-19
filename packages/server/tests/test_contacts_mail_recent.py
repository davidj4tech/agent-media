"""A person's recent mail on their card, read live from notmuch
(contacts_mail.py, §6.25 "Their emails"). notmuch is a fake script answering
made-up JSON; HOME and the state dirs are throwaway, no MEDIA_*/MATRIX_*
setting of this host leaks in, the real ~/Mail is never read, and nothing of
the mail is written anywhere."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

import pytest

from agent_media_server import contacts, contacts_mail, matrix, timeline
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
    timeline._NM.clear()
    contacts._reset_for_tests()

    def nowhere(*a, **k):
        raise AssertionError(f"a real Google request: {a[:2]}")
    monkeypatch.setattr(contacts, "_request", nowhere)
    yield
    timeline._NM.clear()
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
NOMAIL = contacts.contact_id("google", "people/c2")


@pytest.fixture
def book():
    _book(_google(1, "Lionel Smith", ["lionel@example.com"]),
          _google(2, "Ana Lee"))


class Notmuch:
    """A fake notmuch answering `count`, `config get`, `search
    --output=messages` (newest first, up to --limit, matched on the query's
    addresses) and `show` (by id:) from a JSON file, logging each call."""

    def __init__(self, tmp_path, monkeypatch):
        self.dir = tmp_path / "nm"
        self.dir.mkdir()
        self.data = self.dir / "msgs.json"
        self.log = self.dir / "calls.log"
        self.messages: list = []
        self.write()
        script = self.dir / "notmuch"
        script.write_text(f"""#!{sys.executable}
import json, re, sys
args = sys.argv[1:]
open({str(self.log)!r}, "a").write(json.dumps(args) + "\\n")
msgs = json.load(open({str(self.data)!r}))
if args[:1] == ["count"]:
    print(len(msgs))
elif args[:2] == ["config", "get"]:
    print({{"user.primary_email": "david@example.com"}}.get(args[2], ""))
elif args[:1] == ["search"]:
    lim = int(next(a for a in args if a.startswith("--limit=")).split("=")[1])
    addrs = set(re.findall(r"(?:from|to|cc):(\\S+@[^\\s)]+)", args[-1]))
    def hit(m):
        h = m["headers"]
        return any(a in (h["From"] + h["To"] + h["Cc"]).lower() for a in addrs)
    got = sorted((m for m in msgs if hit(m)), key=lambda m: -m["timestamp"])
    print(json.dumps([m["id"] for m in got[:lim]]))
elif args[:1] == ["show"]:
    ids = set(re.findall(r"id:([^\\s)]+)", args[-1]))
    print(json.dumps([[[m, []]] for m in msgs if m["id"] in ids]))
else:
    sys.exit(1)
""")
        script.chmod(0o755)
        monkeypatch.setenv("MEDIA_TIMELINE_NOTMUCH", str(script))
        timeline._NM.clear()

    def add(self, frm, to, *, days_ago=0, subject="Lunch?", tags=(), file_headers=None):
        ts = int((dt.datetime.now() - dt.timedelta(days=days_ago)).timestamp())
        files = []
        if file_headers is not None:
            p = self.dir / f"m{len(self.messages)}.eml"
            p.write_text(file_headers + "\n\nthe body, never read\n")
            files = [str(p)]
        self.messages.append({"id": f"m{len(self.messages)}.x@example", "timestamp": ts,
                              "tags": list(tags), "filename": files, "excluded": False,
                              "headers": {"From": frm, "To": to, "Cc": "",
                                          "Subject": subject, "Date": "x"}})
        self.write()

    def write(self):
        self.data.write_text(json.dumps(self.messages))


@pytest.fixture
def nm(tmp_path, monkeypatch):
    return Notmuch(tmp_path, monkeypatch)


def test_the_card_offers_recent_mail_and_a_mailto(book, nm):
    c = contacts.card(LIONEL)
    assert c["mail"]["recent"] is True
    assert c["mail"]["mailto"] == "mailto:lionel@example.com"
    assert "mail.google.com" in c["mail"]["url"]      # the browser search stays
    assert contacts.card(NOMAIL)["mail"] is None       # no address: nothing


def test_without_notmuch_no_recent(book):
    m = contacts.card(LIONEL)["mail"]
    assert "recent" not in m and m["mailto"]


def test_newest_first_with_a_link_to_each(book, nm, tmp_path):
    nm.add('"Lionel Smith" <lionel@example.com>', "david@example.com", days_ago=3,
           subject="Old one")
    nm.add("David <david@example.com>", "lionel@example.com", days_ago=1, subject="My reply")
    nm.add('"Lionel Smith" <lionel@example.com>', "david@example.com", days_ago=0,
           subject="Newest")
    nm.add("other@example.org", "david@example.com", subject="Not his")
    got = contacts_mail.recent(contacts.card(LIONEL), 5)
    assert [x["subject"] for x in got] == ["Newest", "My reply", "Old one"]
    assert got[0]["from"] == "Lionel Smith" and got[0]["mine"] is False
    assert got[1]["mine"] is True
    assert got[0]["gmail_url"] == ("https://mail.google.com/mail/u/0/#search/"
                                   "rfc822msgid%3Am2.x%40example")
    assert contacts_mail.recent(contacts.card(LIONEL), 2)[-1]["subject"] == "My reply"


def test_lists_machines_spam_and_chats_left_out(book, nm):
    nm.add("lionel@example.com", "david@example.com", subject="Real")
    nm.add("lionel@example.com", "david@example.com", subject="A list",
           file_headers="From: lionel@example.com\nList-Id: <x.example>")
    nm.add("lionel@example.com", "david@example.com", subject="Talk", tags=["chat"])
    got = contacts_mail.recent(contacts.card(LIONEL), 5)
    assert [x["subject"] for x in got] == ["Real"]


def test_nothing_is_written(book, nm, tmp_path):
    nm.add("lionel@example.com", "david@example.com", subject="Secret subject")
    before = {p for p in tmp_path.rglob("*") if p.is_file() and "nm" not in p.parts}
    contacts_mail.recent(contacts.card(LIONEL), 5)
    after = {p for p in tmp_path.rglob("*") if p.is_file() and "nm" not in p.parts}
    assert after == before
    for p in after:
        try:
            assert "Secret subject" not in p.read_text(errors="replace")
        except OSError:
            pass


def test_another_account_cannot(book, nm, monkeypatch):
    monkeypatch.setattr(contacts, "is_owner", lambda: False)
    assert contacts_mail.available() is False


def test_the_route(server, signed_in, book, nm):
    nm.add('"Lionel Smith" <lionel@example.com>', "david@example.com", subject="Hello")
    res, obj = call(server, "GET", f"/contacts/{LIONEL}/mail?limit=20", headers=AUTH)
    assert res.status == 200 and obj["available"] is True
    assert obj["items"][0]["subject"] == "Hello" and obj["limit"] == 20
    res, obj = call(server, "GET", "/contacts/00000000-0000-0000-0000-000000000000/mail",
                    headers=AUTH)
    assert res.status == 404


def test_the_route_without_notmuch(server, signed_in, book):
    res, obj = call(server, "GET", f"/contacts/{LIONEL}/mail", headers=AUTH)
    assert res.status == 200 and obj == {"ok": True, "available": False, "items": []}


def test_the_route_is_gated(server, book, monkeypatch):
    from agent_media_server import auth_abs

    monkeypatch.setattr(auth_abs, "abs_urls", lambda: ["http://abs"])
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: (None, 401))
    res, _ = call(server, "GET", f"/contacts/{LIONEL}/mail")
    assert res.status in (401, 403)
