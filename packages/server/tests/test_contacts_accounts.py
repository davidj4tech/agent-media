"""One address book per person, several per server (contacts proposal,
"Where contacts live"; contract §6.25 "Whose book"). The owner's book is
today's paths; another account's is the same files under `accounts/<key>/`.
Over real HTTP with real device tokens (devices.py, in a throwaway state
dir); no phone, no CRM, no Google, no homeserver."""

from __future__ import annotations

import json
import os

import pytest

from agent_media_core import matrix as core_matrix
from agent_media_server import auth, contacts, devices, matrix, timeline
from test_contract import call, server, typed  # noqa: F401

ISS = "https://cms.test"
ME = "@david:ryer.org"
BOT = "@gmessagesbot:ryer.org"
LIONEL_SMS = "@gmessages_1.42:ryer.org"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("MATRIX_", "MEDIA_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MATRIX_OWNER_BRIDGES", BOT)
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", str(tmp_path / "no-such-adb") + " shell")
    monkeypatch.setattr(matrix, "_ROOMS", {})
    contacts._reset_for_tests()

    def nowhere(*a, **k):
        raise AssertionError(f"a real Google request: {a[:2]}")
    monkeypatch.setattr(contacts, "_request", nowhere)
    yield
    auth._REQ.__dict__.clear()
    contacts._reset_for_tests()


def _google(n, name, phones=()):
    return ("google", f"people/c{n}",
            {"name": name, "names": [{"display": name}],
             "phones": [{"value": contacts.e164(p, "AU"), "raw": p, "type": "Mobile"}
                        for p in phones],
             "emails": [], "addresses": [], "organizations": [], "notes": ""},
            {"etag": f"e{n}"})


def _book(*recs):
    conn = contacts._connect()
    try:
        for kind, sid, card, meta in recs:
            conn.execute("INSERT OR REPLACE INTO records (kind, sid, card, meta) "
                         "VALUES (?, ?, ?, ?)", (kind, sid, json.dumps(card), json.dumps(meta)))
        contacts._rebuild(conn)
    finally:
        conn.close()
    for idx in contacts._INDEXES.values():
        idx["checked"] = 0.0


LIONEL = contacts.contact_id("google", "people/c1")
KAT = contacts.contact_id("google", "people/c9")


def _enrol(sub, email, enrol=False):
    return devices.enrol_account({"iss": ISS, "sub": sub, "email": email}, "phone",
                                 enrol=enrol)["token"]


def _paired():
    code, _ = devices.mint_code("desk")
    return devices.redeem(code, "desk")["token"]


@pytest.fixture
def two_books():
    """The owner's book (Lionel) and Kat's (only her own contact)."""
    _book(_google(1, "Lionel Smith", ["0412 345 678"]))
    kat = auth.account_key_of(ISS, "kat")
    with contacts.as_account(kat):
        _book(_google(9, "Kat's Friend", ["0400 999 888"]))
    return kat


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


# --- whose book a caller reads --------------------------------------------------

def test_the_owners_book_is_todays_paths():
    from agent_media_core._paths import state_dir

    assert contacts.db_path() == state_dir() / "contacts.db"
    assert contacts.token_path() == contacts.config_dir() / "google-contacts-token.json"
    assert "accounts" not in timeline.db_path().parts
    key = auth.account_key_of(ISS, "kat")
    with contacts.as_account(key):
        assert contacts.db_path().parent.name == key
        assert contacts.db_path().parent.parent.name == "accounts"
        assert timeline.db_path().parent.name == key
        assert contacts.token_path().parent.name == key
        assert contacts.client_path() == contacts.config_dir() / "google-oauth-client.json"


def test_who_is_the_owner(monkeypatch):
    paired = {"id": "d_1"}
    davids = {"id": "d_2", "iss": ISS, "sub": "david", "account": "d@x.test", "enrol": True}
    kats = {"id": "d_3", "iss": ISS, "sub": "kat", "account": "k@x.test"}
    assert auth.device_account(paired) == ""                 # paired at the desk
    assert auth.device_account(davids) == ""                 # may enrol: an admin
    assert auth.device_account(kats) == auth.account_key_of(ISS, "kat")
    # Named, the owner is that account alone — an enrol bit is not enough.
    monkeypatch.setenv("MEDIA_OWNER_ACCOUNT", f"{ISS}|email=k@x.test")
    assert auth.device_account(kats) == ""
    assert auth.device_account(davids) == auth.account_key_of(ISS, "david")
    assert auth.account_key({"username": "david", "type": "root"}) == ""
    assert auth.account_key({"username": "guest", "type": "user"}).startswith("abs-")
    assert auth.account_key(None) == auth.NOBODY


def test_an_account_key_is_checked():
    with pytest.raises(ValueError):
        with contacts.as_account("../../etc"):
            pass


# --- the routes -----------------------------------------------------------------

def test_one_account_never_reads_anothers(server, two_books):
    owner, kat = _paired(), _enrol("kat", "k@x.test")
    res, obj = call(server, "GET", "/contacts", headers=H(owner))
    assert res.status == 200 and [c["name"] for c in obj["contacts"]] == ["Lionel Smith"]
    res, obj = call(server, "GET", "/contacts", headers=H(kat))
    assert res.status == 200 and [c["name"] for c in obj["contacts"]] == ["Kat's Friend"]
    res, obj = call(server, "GET", "/contacts?q=lionel", headers=H(kat))
    assert obj["contacts"] == []
    for path in (f"/contacts/{LIONEL}", f"/contacts/{LIONEL}/timeline"):
        res, _ = call(server, "GET", path, headers=H(kat))
        assert res.status == 404, path
    res, _ = call(server, "POST", f"/contacts/{LIONEL}/notes", {"text": "x"}, headers=H(kat))
    assert res.status == 404
    res, _ = call(server, "GET", f"/contacts/{KAT}", headers=H(owner))
    assert res.status == 404
    res, obj = call(server, "GET", f"/contacts/{KAT}", headers=H(kat))
    assert res.status == 200 and obj["contact"]["name"] == "Kat's Friend"


def test_a_note_goes_in_its_writers_book_only(server, two_books):
    owner, kat = _paired(), _enrol("kat", "k@x.test")
    res, obj = call(server, "POST", f"/contacts/{KAT}/notes", {"text": "Kat's own note"},
                    headers=H(kat))
    assert res.status == 200 and obj["ok"]
    res, obj = call(server, "GET", f"/contacts/{KAT}/timeline", headers=H(kat))
    assert [e["summary"] for e in obj["entries"]] == ["Kat's own note"]
    assert not timeline.db_path().exists()                   # the owner's ledger: untouched
    with contacts.as_account(two_books):
        assert timeline.db_path().exists()
    res, obj = call(server, "POST", f"/contacts/{LIONEL}/notes", {"text": "owner's"},
                    headers=H(owner))
    assert res.status == 200
    res, obj = call(server, "GET", f"/contacts/{KAT}/timeline", headers=H(kat))
    assert [e["summary"] for e in obj["entries"]] == ["Kat's own note"]


def test_davids_enrolled_devices_keep_his_book(server, two_books):
    david = _enrol("david", "d@x.test", enrol=True)
    res, obj = call(server, "GET", "/contacts", headers=H(david))
    assert [c["name"] for c in obj["contacts"]] == ["Lionel Smith"]


# --- the Matrix rooms are the owner's ---------------------------------------------

def _room():
    cfg = core_matrix.Config(homeserver="https://hs.test", token="t", rooms={"!a:ryer.org"})
    r = matrix.Room("!a:ryer.org", cfg, ME, get=lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    r.members = {ME: "David", BOT: "bridge bot", LIONEL_SMS: "Lionel Smith"}
    r.network = {"id": "gmessages", "name": "gmessages"}
    r.messages = []
    r.loaded = True
    matrix._ROOMS[r.thread] = r
    return r


def test_the_targets_contact_join_is_the_callers_book(two_books):
    _room()
    auth.set_request_bearer(_paired())
    assert [row.get("contact", {}).get("name") for row in matrix.rows()] == ["Lionel Smith"]
    auth.set_request_bearer(_enrol("kat", "k@x.test"))
    assert [row.get("contact") for row in matrix.rows()] == [None]
    with contacts.as_account(two_books):
        assert contacts.rooms_of(KAT) == []
    auth.set_request_bearer("")                               # nobody: no book at all
    assert [row.get("contact") for row in matrix.rows()] == [None]


def test_another_accounts_book_counts_no_texts_or_calls(monkeypatch, two_books):
    monkeypatch.setenv("MEDIA_TIMELINE_SOURCES", "texts,calls,notes,email")
    assert timeline.enabled() == ["texts", "calls", "notes", "email"]
    with contacts.as_account(two_books):
        # The owner's env is not Kat's; her contacts.json is.
        assert timeline.enabled() == ["notes"]
        assert "android" not in contacts.sources()
        d = contacts.account_config_dir()
        d.mkdir(parents=True)
        (d / "contacts.json").write_text(json.dumps({"timeline": ["texts", "email", "notes"],
                                                     "sources": ["android", "google"]}))
        assert timeline.enabled() == ["email", "notes"]
        assert contacts.sources() == []                       # no phone, not signed in
    monkeypatch.setenv("MEDIA_DRUPAL_CRM", json.dumps({"url": "https://crm.test",
                                                       "client_id": "c", "client_secret": "s"}))
    from agent_media_server import contacts_drupal

    assert contacts_drupal.configured()
    with contacts.as_account(two_books):
        assert not contacts_drupal.configured()               # the owner's CRM is not hers


# --- the loop and the command line -------------------------------------------------

def test_the_loop_syncs_every_book(monkeypatch, two_books):
    seen = []
    monkeypatch.setattr(contacts, "sync", lambda full=False: (seen.append(contacts.account()),
                                                              {"synced": [], "errors": {}})[1])
    monkeypatch.setattr(timeline, "rollup", lambda: {"changed": 0, "errors": 0,
                                                     "source_errors": {}, "pushed": 0})
    assert contacts.accounts() == ["", two_books]
    for key in contacts.accounts():
        with contacts.as_account(key):
            contacts._sync_book(key)
    assert seen == ["", two_books]


def test_the_cli_is_the_owner_unless_told(two_books, capsys):
    _enrol("kat", "k@x.test")
    assert contacts.main(["note", "lionel", "owner's note"]) == 0
    assert contacts.main(["--account", "k@x.test", "note", "lionel", "x"]) == 1
    assert "no contact matches" in capsys.readouterr().err
    assert contacts.main(["--account", "k@x.test", "note", "friend", "hers"]) == 0
    assert contacts.main(["--account", two_books, "timeline", "friend"]) == 0
    assert "hers" in capsys.readouterr().out
    assert contacts.main(["--account", "nobody@x.test", "status"]) == 2
    assert contacts.main(["accounts"]) == 0
    out = capsys.readouterr().out
    assert "owner" in out and f"{two_books}  k@x.test (1 device(s))" in out
