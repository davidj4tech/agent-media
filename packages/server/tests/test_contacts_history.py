"""Past conversations from the networks' exports, into the timeline
(contacts_history.py, timeline.import_days, `contacts import`, `POST
/contacts/import`, §6.25 "Exports", §6.25a). The exports are small made-up
files; HOME, the config dir and the state dir are throwaway, and no MEDIA_*
or MATRIX_* setting of this host leaks in."""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import sqlite3
import zipfile

import pytest

from agent_media_server import (contacts, contacts_exports, contacts_history, contacts_import,
                                matrix, timeline)
from test_contract import AUTH, server, signed_in, typed  # noqa: F401

ME, SAM = "@david:ryer.org", "@sam:ryer.org"


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

    def nowhere(*a, **k):
        raise AssertionError(f"a real request: {a[:2]}")
    monkeypatch.setattr(contacts, "_request", nowhere)
    yield
    contacts._reset_for_tests()


def _google(n, name, emails=()):
    return ("google", f"people/c{n}",
            {"name": name, "names": [{"display": name, "given": "", "family": ""}],
             "phones": [], "emails": [{"value": e, "type": "home"} for e in emails],
             "addresses": [], "organizations": [], "notes": ""},
            {"etag": f"e{n}"})


def _bridged(ghost, name, network="facebookgo"):
    card = {"name": name, "names": [{"display": name, "given": "", "family": ""}],
            "phones": [], "emails": [], "addresses": [], "organizations": [], "notes": "",
            "handles": [{"network": network, "network_name": "Messenger", "id": ghost}]}
    return ("bridges", ghost, card, {"network": network, "network_name": "Messenger"})


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
ALEX_PHONE = contacts.contact_id("google", "people/c2")
ALEX_MESSENGER = contacts.contact_id("bridges", "@facebook_9:ryer.org")


@pytest.fixture
def book():
    _book(_google(1, "Lionel Smith", ["lionel@example.com",
                                      "lionel%hotmail.com@msn.gajim.org"]),
          _google(2, "Alex Chen"), _bridged("@facebook_9:ryer.org", "Alex Chen"))


def _mojibake(s: str) -> str:
    return s.encode("utf-8").decode("latin-1")


def _ms(y, m, d, h=12):
    return int(dt.datetime(y, m, d, h).timestamp() * 1000)


def _day(ms):
    return dt.date.fromtimestamp(ms / 1000).isoformat()


D1, D2, D3 = _ms(2015, 3, 1), _ms(2015, 3, 2), _ms(2016, 7, 9)


def _thread(people, msgs):
    return {"participants": [{"name": p} for p in people],
            "messages": [{"sender_name": s, "timestamp_ms": t, "content": "never read"}
                         for s, t in msgs]}


FB = {
    "your_facebook_activity/messages/inbox/lionel_1/message_1.json":
        _thread(["Lionel Smith", "David Ryer"],
                [("Lionel Smith", D1), ("David Ryer", D1), ("David Ryer", D1), ("Lionel Smith", D2)]),
    # A thread split over two files, and the archive: summed per day.
    "your_facebook_activity/messages/inbox/lionel_1/message_2.json":
        _thread(["Lionel Smith", "David Ryer"], [("Lionel Smith", D1)]),
    "your_facebook_activity/messages/inbox/alex_2/message_1.json":
        _thread(["Alex Chen", "David Ryer"], [("Alex Chen", D3)]),
    "your_facebook_activity/messages/archived_threads/jose_3/message_1.json":
        _thread([_mojibake("José Núñez"), "David Ryer"],
                [(_mojibake("José Núñez"), D3), ("David Ryer", D3)]),
    "your_facebook_activity/messages/inbox/group_4/message_1.json":
        _thread(["Lionel Smith", "Alex Chen", "David Ryer"], [("Lionel Smith", D2)]),
    "your_facebook_activity/messages/message_requests/stranger_5/message_1.json":
        _thread(["Pushy Stranger", "David Ryer"], [("Pushy Stranger", D2)]),
    "your_facebook_activity/messages/inbox/lionel_1/photos/1.jpg": b"\xff\xd8 not read",
}


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, obj in files.items():
            zf.writestr(name, obj if isinstance(obj, bytes) else json.dumps(obj))
    return buf.getvalue()


def _entries():
    conn = sqlite3.connect(str(timeline.db_path()))
    try:
        return {(r[0], r[1], r[2]): (r[3], r[4], r[5]) for r in conn.execute(
            "SELECT contact, channel, day, count_in, count_out, via FROM entries")}
    finally:
        conn.close()


def _people():
    conn = contacts._connect()
    try:
        return {name: json.loads(raw) for name, raw in conn.execute("SELECT name, card FROM contacts")}
    finally:
        conn.close()


def test_messenger_threads_become_days_one_to_one_only(book):
    got = contacts.import_export(_zip(FB), "facebook-david.zip")
    h = got["history"]
    assert h["messenger_conversations"] == 3 and h["groups_left_out"] == 1
    assert got["imported"]["messenger_conversations"] == 3
    e = _entries()
    # Two files of one thread summed per day; David's own messages are out.
    assert e[(LIONEL, "messenger", _day(D1))] == (2, 2, "import")
    assert e[(LIONEL, "messenger", _day(D2))] == (1, 0, "import")
    # Two Alex Chens: the one known on Messenger takes it.
    assert (ALEX_MESSENGER, "messenger", _day(D3)) in e
    assert not any(k[0] == ALEX_PHONE for k in e)
    # José is no one here: an import person of his own, his name fixed.
    jose = _people()["José Núñez"]
    assert "Messenger history" in jose["labels"]
    assert (contacts.contact_id("import", contacts_history._sid("messenger", "José Núñez")),
            "messenger", _day(D3)) in e
    # Message requests are not conversations, and no group day was counted.
    assert "Pushy Stranger" not in _people()
    assert sum(v[0] + v[1] for v in e.values()) == 2 + 2 + 1 + 1 + 2


def test_the_same_export_again_changes_nothing_and_a_count_never_falls(book):
    contacts.import_export(_zip(FB), "facebook.zip")
    before = _entries()
    again = contacts.import_export(_zip(FB), "facebook.zip")
    assert again["history"]["days"] == 0 and _entries() == before
    # A day the rollup counted higher keeps the higher count.
    key = (LIONEL, "messenger", _day(D2))
    timeline.import_days({key: [5, 1]})
    contacts.import_export(_zip(FB), "facebook.zip")
    assert _entries()[key][:2] == (5, 1)
    conn = contacts._connect()
    try:
        names = [n for (n,) in conn.execute("SELECT name FROM contacts")]
    finally:
        conn.close()
    assert names.count("José Núñez") == 1            # no second history person


def test_instagram_dms_by_their_path(book):
    ig = {"your_instagram_activity/messages/inbox/lionelsmith_77/message_1.json":
          _thread(["Lionel Smith", "david.r"], [("Lionel Smith", D2), ("david.r", D2)]),
          "your_instagram_activity/messages/inbox/other_78/message_1.json":
          _thread(["Alex Chen", "david.r"], [("david.r", D3)])}
    got = contacts.import_export(_zip(ig), "instagram-david.zip")
    assert got["history"]["instagram_conversations"] == 2
    assert _entries()[(LIONEL, "instagram", _day(D2))] == (1, 1, "import")


def test_who_david_is_from_the_setting_when_one_thread_cannot_tell(book, monkeypatch):
    one = {"messages/inbox/lionel_1/message_1.json":
           _thread(["Lionel Smith", "David Ryer"], [("Lionel Smith", D1)])}
    got = contacts.import_export(_zip(one), "facebook.zip")
    assert got["history"]["unknown_self"] == 1 and not _entries()
    monkeypatch.setenv("MEDIA_CONTACTS_SELF_NAME", "David Ryer")
    got = contacts.import_export(_zip(one), "facebook.zip")
    assert got["history"]["messenger_conversations"] == 1
    assert _entries()[(LIONEL, "messenger", _day(D1))][:2] == (1, 0)


def _mail(frm, to, when, labels="Chat"):
    lab = f"X-Gmail-Labels: {labels}\n" if labels is not None else ""
    return (f"From 123@xxx {when}\n{lab}From: {frm}\nTo: {to}\nDate: {when}\n"
            f"Subject: Chat with someone\n\nnever read\n>From the body, escaped as mboxrd does\n\n")


CHATS = "".join([
    _mail("David <david@example.com>", "Lionel <lionel@example.com>", "Sun, 1 Mar 2009 10:00:00 +1100"),
    _mail("Lionel <lionel@example.com>", "David <david@example.com>", "Sun, 1 Mar 2009 10:01:00 +1100"),
    # An old gateway address: matched through the card's old chat addresses.
    _mail("lionel%hotmail.com@msn.gajim.org/Resource", "david@example.com",
          "Mon, 2 Mar 2009 09:00:00 +1100"),
    _mail("nobody@nowhere.example", "david@example.com", "Mon, 2 Mar 2009 09:30:00 +1100"),
    _mail("David <david@example.com>", "Lionel <lionel@example.com>",
          "Tue, 3 Mar 2009 09:00:00 +1100", labels="Inbox,Important"),
    _mail("David <david@example.com>", "lionel@example.com", "Wed, 4 Mar 2009 09:00:00 +1100"),
]).encode()


def _local(s):
    return dt.datetime.strptime(s, "%a, %d %b %Y %H:%M:%S %z").astimezone().date().isoformat()


def test_gmail_chats_by_address_and_old_gateway_address(book, tmp_path):
    path = tmp_path / "Chats.mbox"
    path.write_bytes(CHATS)
    got = contacts.import_path(path)
    h = got["history"]
    assert h["old_chats"] == 1 and h["unmatched"] == 1 and h["chats_without_the_label"] == 1
    e = _entries()
    assert e[(LIONEL, "chat", _local("Sun, 1 Mar 2009 10:00:00 +1100"))] == (1, 1, "import")
    assert e[(LIONEL, "chat", _local("Mon, 2 Mar 2009 09:00:00 +1100"))] == (1, 0, "import")
    assert (LIONEL, "chat", _local("Tue, 3 Mar 2009 09:00:00 +1100")) not in e


def test_takeout_zip_read_as_it_goes(book, tmp_path):
    path = tmp_path / "takeout-20261019.zip"
    path.write_bytes(_zip({"Takeout/Mail/Chats.mbox": CHATS,
                           "Takeout/archive_browser.html": b"<html>"}))
    got = contacts.import_path(path)
    assert got["imported"] == {"old_chats": 1}


def test_a_zip_of_both_lists_and_messages(book):
    files = dict(FB)
    files["your_facebook_activity/connections/friends/your_friends.json"] = {
        "friends_v2": [{"name": "Ana Stranger", "timestamp": 1500000200}]}
    got = contacts.import_export(_zip(files), "facebook.zip")
    assert got["imported"]["facebook_friends"] == 1
    assert got["imported"]["messenger_conversations"] == 3


def test_nothing_known_is_refused(book):
    with pytest.raises(contacts_import.NotAnExport):
        contacts.import_export(_zip({"your_facebook_activity/other.json": {}}), "x.zip")
    with pytest.raises(contacts_import.NotAnExport):
        contacts.import_export(b"From x\nX-Gmail-Labels: Inbox\nFrom: a@b.c\n\n", "x.mbox")


def test_the_cli_says_counts_never_names(book, tmp_path, capsys):
    path = tmp_path / "facebook.zip"
    path.write_bytes(_zip(FB))
    assert contacts.main(["import", str(path)]) == 0
    out = capsys.readouterr().out
    assert "3 messenger conversations" in out and "history:" in out
    assert "Lionel" not in out and "José" not in out


def test_the_folder_import_takes_any_size(book, tmp_path, monkeypatch):
    folder = tmp_path / "imports"
    folder.mkdir()
    (folder / "facebook-big.zip").write_bytes(_zip(FB))
    monkeypatch.setenv("MEDIA_CONTACTS_IMPORT_DIR", str(folder))
    monkeypatch.setattr(contacts, "IMPORT_MAX", 10)          # far smaller than the file
    monkeypatch.setattr(contacts_exports, "_alert", lambda body: None)
    got = contacts_exports.watch()
    assert got["imported"] == ["facebook-big.zip"] and not got["errors"]
    assert (LIONEL, "messenger", _day(D1)) in _entries()


def test_export_links_name_the_conversations():
    links = {x["network"]: x for x in contacts_exports.export_links()}
    assert {"facebook", "instagram", "messenger", "instagram_messages", "chats"} <= set(links)
    assert "Messages only" in links["messenger"]["steps"]
    assert links["chats"]["url"].startswith("https://takeout.google.com")
    assert "Chats label" in links["chats"]["steps"]


def test_the_route_takes_an_mbox(server, signed_in, book):
    import http.client

    conn = http.client.HTTPConnection(*server, timeout=10)
    conn.request("POST", "/contacts/import?name=Chats.mbox", body=CHATS,
                 headers={**AUTH, "Content-Type": "application/mbox"})
    res = conn.getresponse()
    obj = json.loads(res.read())
    conn.close()
    assert res.status == 200 and obj["imported"] == {"old_chats": 1}
    assert "Lionel" not in json.dumps(obj)
