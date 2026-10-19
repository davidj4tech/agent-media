"""The email timeline from this host's notmuch index, and IMAP's password
from a file or a command (timeline.py, §6.25a "email"). notmuch is a fake
script answering made-up JSON; HOME, the config and state dirs are
throwaway, no MEDIA_*/MATRIX_* setting of this host leaks in, and the real
~/Mail is never read. Only counts may reach the ledger."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys

import pytest

from agent_media_server import alerts, contacts, contacts_exports, matrix, timeline

TODAY = dt.date.today().isoformat()


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
    monkeypatch.setenv("MEDIA_ALERTS_INBOX", "0")
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", str(tmp_path / "no-such-adb") + " shell")
    monkeypatch.setenv("MEDIA_TIMELINE_NOTMUCH", str(tmp_path / "no-such-notmuch"))
    monkeypatch.setattr(matrix, "_ROOMS", {})
    timeline._NM.clear()
    contacts._reset_for_tests()

    def nowhere(*a, **k):
        raise AssertionError(f"a real Google request: {a[:2]}")
    monkeypatch.setattr(contacts, "_request", nowhere)

    def no_mail(cfg):
        raise AssertionError("a real IMAP connection")
    monkeypatch.setattr(timeline, "_imap", no_mail)
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
MEL = contacts.contact_id("google", "people/c2")


@pytest.fixture
def book():
    _book(_google(1, "Lionel Smith", ["lionel@example.com",
                                      "lionel%hotmail.example@msn.gajim.org"]),
          _google(2, "Mel Jones", ["mel@example.org"]),
          _google(3, "News Letter", ["news@example.net"]))


class Notmuch:
    """A fake notmuch: a script that answers `count`, `config get` and
    `show` from a JSON file and logs each call's arguments."""

    def __init__(self, tmp_path, monkeypatch):
        self.dir = tmp_path / "nm"
        self.dir.mkdir()
        self.data = self.dir / "show.json"
        self.log = self.dir / "calls.log"
        self.messages: list = []
        self.write()
        script = self.dir / "notmuch"
        script.write_text(f"""#!{sys.executable}
import json, sys
args = sys.argv[1:]
open({str(self.log)!r}, "a").write(json.dumps(args) + "\\n")
if args[:1] == ["count"]:
    print(len(json.load(open({str(self.data)!r}))))
elif args[:2] == ["config", "get"]:
    print({{"user.primary_email": "david@example.com",
           "user.other_email": "dj@example.com;other@example.com"}}.get(args[2], ""))
elif args[:1] == ["show"]:
    import re
    msgs = json.load(open({str(self.data)!r}))
    m = re.search(r"date:@(\\d+)\\.\\.(?:@(\\d+))?", args[-1])
    lo, hi = (int(m.group(1)), int(m.group(2) or 2**40)) if m else (0, 2**40)
    print(json.dumps([[[x, []]] for x in msgs if lo <= x["timestamp"] <= hi]))
else:
    sys.exit(1)
""")
        script.chmod(0o755)
        monkeypatch.setenv("MEDIA_TIMELINE_NOTMUCH", str(script))
        timeline._NM.clear()

    def add(self, frm, to, cc="", *, day=TODAY, tags=(), file_headers=None, mid=None,
            subject="Lunch?"):
        ts = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(12)).timestamp()
        files = []
        if file_headers is not None:
            p = self.dir / f"m{len(self.messages)}.eml"
            p.write_text(file_headers + "\n\nthe body, never read\n")
            files = [str(p)]
        self.messages.append({"id": mid or f"m{len(self.messages)}@example", "timestamp": int(ts),
                              "tags": list(tags), "filename": files, "excluded": False,
                              "headers": {"From": frm, "To": to, "Cc": cc, "Subject": subject,
                                          "Date": "x"}})
        self.write()

    def write(self):
        self.data.write_text(json.dumps(self.messages))

    def calls(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] \
            if self.log.exists() else []


@pytest.fixture
def nm(tmp_path, monkeypatch):
    return Notmuch(tmp_path, monkeypatch)


def _ledger():
    conn = timeline._connect()
    try:
        return {(r[0], r[1], r[2]): r[3:] for r in conn.execute(
            "SELECT contact, channel, day, count_in, count_out FROM entries")}
    finally:
        conn.close()


def test_notmuch_is_preferred_when_it_has_a_database(nm):
    assert timeline.mail_backend() == "notmuch"
    os.environ["MEDIA_TIMELINE_MAIL"] = "imap"
    try:
        assert timeline.mail_backend() == "imap"
    finally:
        del os.environ["MEDIA_TIMELINE_MAIL"]


def test_no_notmuch_is_imap(monkeypatch):
    assert timeline.mail_backend() == "imap"
    monkeypatch.setenv("MEDIA_TIMELINE_MAIL", "off")
    assert timeline.mail_backend() == "off"
    assert timeline.email_([TODAY]) == []


def test_notmuch_counts_in_and_out_by_the_rule(book, nm, tmp_path):
    (tmp_path / "home" / ".mbsyncrc").write_text(
        "IMAPAccount a\nHost imap.gmail.com\nUser Work@Example.com\nPassCmd \"cat x\"\n")
    nm.add("Lionel <lionel@example.com>", "David <david@example.com>")          # in
    nm.add("david@example.com", "Lionel@Example.com, mel@example.org")          # out ×2
    nm.add("work@example.com", "mel@example.org")                               # out (mbsync's)
    nm.add("dj@example.com", "lionel@example.com", cc="lionel@example.com")     # once per contact
    nm.add("lionel@example.com", "someone-else@example.com")                    # Bcc'd: no
    nm.add("Lionel <lionel@hotmail.example>", "other@example.com")              # decoded alias
    nm.add("lionel%hotmail.example@msn.gajim.org/Gajim", "david@example.com",
           tags=["chat"])                                                       # Google Talk: no
    nm.add("noreply@example.net", "david@example.com")                          # no-reply: no
    nm.add("news@example.net", "david@example.com",
           file_headers="From: news@example.net\nList-Id: <news.example.net>")  # a list: no
    os.environ["MEDIA_TIMELINE_SOURCES"] = "email"
    try:
        got = timeline.rollup(push_=False)
    finally:
        del os.environ["MEDIA_TIMELINE_SOURCES"]
    assert got["source_errors"] == {}
    led = _ledger()
    assert led[(LIONEL, "email", TODAY)] == (2, 2)
    assert led[(MEL, "email", TODAY)] == (0, 2)
    assert not any(k[0] == contacts.contact_id("google", "people/c3") for k in led)
    shows = [c for c in nm.calls() if c[:1] == ["show"]]
    assert shows and all("--body=false" in c and "not tag:spam" in c[-1] for c in shows)


def test_the_ledger_holds_only_counts(book, nm):
    nm.add("Lionel <lionel@example.com>", "david@example.com",
           mid="secret-id-123@example", subject="Private: the surprise party")
    os.environ["MEDIA_TIMELINE_SOURCES"] = "email"
    try:
        timeline.rollup(push_=False)
    finally:
        del os.environ["MEDIA_TIMELINE_SOURCES"]
    assert _ledger()[(LIONEL, "email", TODAY)] == (1, 0)
    conn = sqlite3.connect(timeline.db_path())
    try:
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    for word in ("secret-id-123", "surprise", "Private", "lionel@example.com"):
        assert word not in dump


def test_a_long_window_is_asked_a_month_at_a_time(book, nm):
    window = timeline.days(90)
    nm.add("lionel@example.com", "david@example.com", day=window[0])
    got = timeline._email_notmuch(window)
    assert got == [(LIONEL, "email", window[0], "in", 0)]
    shows = [c for c in nm.calls() if c[:1] == ["show"]]
    assert 3 <= len(shows) <= 4


def test_imap_password_from_a_file_or_a_command(tmp_path, monkeypatch):
    secret = tmp_path / "home" / ".mail-secrets" / "me"
    secret.parent.mkdir(parents=True)
    secret.write_text("app pw 1234\n")
    monkeypatch.setenv("MEDIA_TIMELINE_IMAP", json.dumps(
        {"host": "imap.example", "user": "me@example.com",
         "password_file": "~/.mail-secrets/me"}))
    assert timeline.imap_settings()["password"] == "app pw 1234"
    monkeypatch.setenv("MEDIA_TIMELINE_IMAP", json.dumps(
        {"host": "imap.example", "user": "me@example.com",
         "password_cmd": "cat ~/.mail-secrets/me"}))
    assert timeline.imap_settings()["password"] == "app pw 1234"
    monkeypatch.setenv("MEDIA_TIMELINE_IMAP", json.dumps(
        {"host": "imap.example", "user": "me@example.com", "password_cmd": "false"}))
    assert timeline.imap_settings() is None


def _alert(aid):
    return next((a for a in alerts.list_alerts() if a.get("id") == aid), {}) \
        if hasattr(alerts, "list_alerts") else {}


def test_the_ready_mail_through_notmuch(nm, monkeypatch):
    monkeypatch.setenv("MEDIA_CONTACTS_EXPORT_MAIL", "1")
    nm.add("Facebook <notification@facebookmail.com>", "david@example.com",
           subject="Your Facebook information is ready to download",
           file_headers="From: notification@facebookmail.com\nSubject: ready\n"
                        "Content-Type: text/plain\n\n"
                        "Get it https://accountscenter.facebook.com/info_and_permissions/dyi/?id=7")
    nm.add("Phish <x@facebookmail.com.evil.example>", "david@example.com",
           subject="Your information is ready")
    got = contacts_exports.mail()
    assert got == ["contacts.export.ready.facebook"]
    assert contacts_exports.mail() == []                     # each mail once
    q = [c for c in nm.calls() if c[:1] == ["show"]][0][-1]
    assert "from:facebookmail.com" in q and "from:mail.instagram.com" in q
