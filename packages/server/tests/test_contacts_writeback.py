"""A Join writes the person's links to their Google entry, through a stand-in
phone (contacts_writeback.py). Nothing here reaches the real one: the adb
command is a script in tmp_path that answers and logs."""

import json
import os
import shlex
import sys

import pytest

from agent_media_server import contacts, contacts_writeback

FB = "@facebook_100000003184761:ryer.org"
WA = "@whatsapp_61400111222:ryer.org"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("MATRIX_", "MEDIA_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", str(tmp_path / "no-such-adb") + " shell")
    contacts._reset_for_tests()

    class Now:                       # the write runs before same() returns
        def __init__(self, target, **_):
            self.target = target

        def start(self):
            self.target()
    monkeypatch.setattr(contacts_writeback.threading, "Thread", Now)
    yield
    contacts._reset_for_tests()


def phone(monkeypatch, tmp_path, *, google=True, websites=(), refuse=False):
    """A stand-in `agent-phone-adb shell`: logs each script, answers the
    raw-contact and website queries."""
    log = tmp_path / "phone.log"
    raw = "Row: 0 _id=12\n" if google else "No result found.\n"
    sites = "".join(f"Row: {i} _id={90 + i}, data1={u}\n" for i, u in enumerate(websites)) \
        or "No result found.\n"
    err = ("Error while accessing provider:com.android.contacts\n"
           "java.lang.IllegalArgumentException: no\n") if refuse else ""
    script = tmp_path / "adb"
    script.write_text(f"""#!{sys.executable}
import sys
s = sys.argv[-1]
open({str(log)!r}, "a").write(s + "\\n")
if "/raw_contacts" in s:
    print({raw!r}, end="")
elif s.startswith("content query"):
    print({sites!r}, end="")
elif s.startswith("content insert"):
    print({err!r}, end="")
""")
    script.chmod(0o755)
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", f"{script} shell")
    return log


def _book(*handles, accounts=("com.google", "com.whatsapp"), metas=None):
    person = {"name": "Peter Ryan", "names": [{"display": "Peter Ryan", "given": "Peter",
                                               "family": "Ryan"}],
              "phones": [], "emails": [], "addresses": [], "organizations": [], "notes": ""}
    recs = [("android", "lookup-1", person,
             {"raw": [11, 12], "accounts": list(accounts), "version": 3})]
    for i, h in enumerate(handles):
        recs.append(("bridges", h["id"],
                     {**person, "name": f"Ghost {i}", "names": [], "handles": [h]},
                     {"network": h["network"], "network_name": h["network_name"],
                      **((metas or {}).get(h["id"]) or {})}))
    conn = contacts._connect()
    try:
        for kind, sid, card, meta in recs:
            conn.execute("INSERT INTO records (kind, sid, card, meta) VALUES (?, ?, ?, ?)",
                         (kind, sid, json.dumps(card), json.dumps(meta)))
        contacts._rebuild(conn)
        ids = {n: cid for cid, n in conn.execute("SELECT id, name FROM contacts")}
    finally:
        conn.close()
    return ids


def _fb():
    return {"network": "facebookgo", "network_name": "Facebook Messenger", "id": FB}


def _state():
    return json.loads(contacts_writeback.path().read_text())


def test_links_from_handles():
    card = {"handles": [
        _fb(),
        {"network": "whatsapp", "network_name": "WhatsApp", "id": WA},
        {"network": "whatsapp", "network_name": "WhatsApp", "id": "@whatsapp_lid-123:ryer.org"},
        {"network": "instagram", "network_name": "Instagram", "id": "instagram:pete.r",
         "username": "pete.r"},
        {"network": "instagramgo", "network_name": "Instagram",
         "id": "@instagram_112243413503777:ryer.org"},
        {"network": "signal", "network_name": "Signal", "id": "@signal_abc:ryer.org"},
    ]}
    assert contacts_writeback.links(card) == [
        ("Facebook", "https://www.facebook.com/profile.php?id=100000003184761"),
        ("WhatsApp", "https://wa.me/61400111222"),
        ("Instagram", "https://www.instagram.com/pete.r/"),
    ]


def test_a_join_adds_the_facebook_link_to_the_google_raw_contact(monkeypatch, tmp_path):
    ids = _book(_fb())
    log = phone(monkeypatch, tmp_path)
    contacts.same(ids["Ghost 0"], ids["Peter Ryan"])
    scripts = log.read_text().splitlines()
    assert "_id IN (11,12) AND deleted=0 AND account_type='com.google'" in scripts[0]
    insert = [s for s in scripts if s.startswith("content insert")]
    assert insert == [
        "content insert --uri content://com.android.contacts/data "
        "--bind raw_contact_id:i:12 --bind mimetype:s:vnd.android.cursor.item/website "
        "--bind 'data1:s:https://www.facebook.com/profile.php?id=100000003184761' "
        "--bind data2:i:0 --bind data3:s:Facebook"]
    done = _state()["done"][ids["Peter Ryan"]]
    assert done["status"] == "added" and done["raw"] == "12"
    assert _state()["pending"] == {}


def test_a_link_already_there_is_not_added_again(monkeypatch, tmp_path):
    ids = _book(_fb())
    log = phone(monkeypatch, tmp_path,
                websites=["http://facebook.com/profile.php?id=100000003184761/"])
    contacts.same(ids["Ghost 0"], ids["Peter Ryan"])
    assert not [s for s in log.read_text().splitlines() if s.startswith("content insert")]
    assert _state()["done"][ids["Peter Ryan"]]["status"] == "nothing"


def test_no_google_entry_is_let_go(monkeypatch, tmp_path):
    ids = _book(_fb(), accounts=("com.whatsapp",))
    log = phone(monkeypatch, tmp_path)
    contacts.same(ids["Ghost 0"], ids["Peter Ryan"])
    assert not log.exists()            # not even asked
    assert _state()["done"][ids["Peter Ryan"]]["status"] == "no-google"


def test_a_refusal_is_said_and_not_retried(monkeypatch, tmp_path):
    ids = _book(_fb())
    phone(monkeypatch, tmp_path, refuse=True)
    contacts.same(ids["Ghost 0"], ids["Peter Ryan"])
    done = _state()["done"][ids["Peter Ryan"]]
    assert done["status"] == "failed" and "IllegalArgumentException" in done["error"]
    assert _state()["pending"] == {}


def test_the_phone_away_keeps_it_queued_for_the_sync_loop(monkeypatch, tmp_path):
    ids = _book(_fb())
    away = tmp_path / "away"
    away.write_text("#!/bin/sh\necho 'adb: device offline' >&2\nexit 1\n")
    away.chmod(0o755)
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", f"{away} shell")
    contacts.same(ids["Ghost 0"], ids["Peter Ryan"])
    p = _state()["pending"][ids["Peter Ryan"]]
    assert p["tries"] == 1 and "device offline" in p["last_error"]
    phone(monkeypatch, tmp_path)
    assert contacts_writeback.flush() == {"written": 1, "waiting": 0}
    assert _state()["done"][ids["Peter Ryan"]]["status"] == "added"


def test_no_phone_tool_does_nothing():
    ids = _book(_fb())
    contacts.same(ids["Ghost 0"], ids["Peter Ryan"])      # no-such-adb
    assert not contacts_writeback.path().exists()


def test_off_does_nothing(monkeypatch, tmp_path):
    ids = _book(_fb())
    log = phone(monkeypatch, tmp_path)
    monkeypatch.setenv("MEDIA_CONTACTS_WRITEBACK", "0")
    contacts.same(ids["Ghost 0"], ids["Peter Ryan"])
    assert not log.exists() and not contacts_writeback.path().exists()


def test_every_bridged_account_gets_a_card_link_with_its_id(monkeypatch, tmp_path):
    sig = {"network": "signal", "network_name": "Signal", "id": "@signal_abc:ryer.org"}
    ig = {"network": "instagramgo", "network_name": "Instagram",
          "id": "@instagram_112243413503777:ryer.org"}
    ids = _book(_fb(), sig, ig, metas={
        FB: {"rooms": ["!fb:ryer.org"]},
        sig["id"]: {"rooms": ["!sig:ryer.org"]},
        ig["id"]: {"rooms": ["!group:ryer.org"], "groups_only": True}})
    monkeypatch.setenv("MEDIA_PUBLIC_URL", "https://red5.example/")
    log = phone(monkeypatch, tmp_path)
    for g in ("Ghost 0", "Ghost 1", "Ghost 2"):
        contacts.same(ids[g], ids["Peter Ryan"])
    rows = set()
    for s in log.read_text().splitlines():
        if s.startswith("content insert"):
            b = {x.split(":s:")[0]: x.split(":s:")[1] for x in shlex.split(s) if ":s:" in x}
            rows.add((b["data3"], b["data1"]))
    card = f"https://red5.example/app/people/{ids['Peter Ryan']}?account="
    assert rows == {
        ("Facebook", "https://www.facebook.com/profile.php?id=100000003184761"),
        ("Facebook Messenger", card + FB),
        ("Signal", card + "@signal_abc:ryer.org"),
        ("Instagram (groups)", card + "@instagram_112243413503777:ryer.org"),
    }
