"""Uploaded contacts (contacts_import.py, the `upload` records): the address
books David once let Facebook and Instagram sync. They make no one; a
network's person named as an entry joins the one contact holding its number
or address. Made-up exports only; HOME, the config and state dirs are
throwaway, and no MEDIA_* or MATRIX_* setting of this host leaks in."""

from __future__ import annotations

import io
import json
import os
import zipfile

import pytest

from agent_media_server import contacts, contacts_exports, contacts_import, matrix

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

    def nowhere(*a, **k):
        raise AssertionError(f"a real Google request: {a[:2]}")
    monkeypatch.setattr(contacts, "_request", nowhere)
    yield
    contacts._reset_for_tests()


def _card(name, phones=(), emails=(), handles=None):
    c = {"name": name, "names": [{"display": name, "given": "", "family": ""}],
         "phones": [{"value": contacts.e164(p, "AU"), "raw": p} for p in phones],
         "emails": [{"value": e} for e in emails], "addresses": [], "organizations": [],
         "notes": ""}
    if handles:
        c["handles"] = handles
    return c


def _google(n, name, phones=(), emails=()):
    return ("google", f"people/c{n}", _card(name, phones, emails), {"etag": f"e{n}"})


def _ghost(uid, name, network="facebookgo", network_name="Messenger"):
    return ("bridges", uid, _card(name, handles=[{"network": network,
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


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path, obj in files.items():
            z.writestr(path, obj if isinstance(obj, (str, bytes)) else json.dumps(obj))
    return buf.getvalue()


def _mojibake(s: str) -> str:
    return s.encode("utf-8").decode("latin-1")


DAD = contacts.contact_id("google", "people/c1")
FB_LIONEL = "@facebook_1000111:ryer.org"
IG_LIONEL = "@instagram_2000111:ryer.org"

FB_BOOK = {"address_book_v2": {"address_book": [
    {"name": "Lionel Smith", "details": [{"contact_point": "0412 345 678"}],
     "created_timestamp": 1400000000},
    {"name": _mojibake("José Núñez"), "details": [{"contact_point": "jose@example.org"}]},
    {"name": "No Points", "details": []},
]}}
FB_OLD = {"address_book": {"address_book": [
    {"name": "Old Entry", "details": [{"contact_point": "+61 400 000 001"}]}]}}
FB_NEWEST = [{"label_values": [
    {"label": "Name", "value": "Newest Entry"},
    {"label": "Contact points", "vec": [{"value": "+61 400 000 002"}]}]}]
IG_BOOK = {"contacts_contact_info": [
    {"string_map_data": {"First name": {"value": "Lionel"}, "Surname": {"value": "Smith"},
                         "Contact information": {"value": "+61412345678"}}},
    {"string_map_data": {"First name": {"value": "Mail"}, "Last name": {"value": "Only"},
                         "Contact information": {"value": "mail.only@example.org"}}}]}


@pytest.fixture
def book():
    # David's own name for his father, and the people Messenger knows.
    _book(_google(1, "Dad", ["0412 345 678"]), _google(2, "Ana Lee", ["0400 999 000"]),
          _ghost(FB_LIONEL, "Lionel Smith"))


def _card_of(cid):
    return contacts.card(cid)


# --- reading -----------------------------------------------------------------------

def test_facebook_address_books_every_shape():
    got, counts = contacts_import.parse(_zip({
        "personal_information/other_personal_information/your_address_books.json": FB_BOOK}),
        "facebook-david.zip")
    assert counts == {"facebook_uploads": 2}            # no points: left out
    entries = sorted(r["card"]["name"] for r in got["facebook_uploads"].values())
    assert entries == ["José Núñez", "Lionel Smith"]
    lionel = next(r for r in got["facebook_uploads"].values() if r["card"]["name"] == "Lionel Smith")
    assert lionel["card"]["phones"] == [{"value": "+61412345678"}]
    assert lionel["meta"] == {"export": "facebook", "upload": True}
    _, c = contacts_import.parse(json.dumps(FB_OLD).encode(), "your_address_books.json")
    assert c == {"facebook_uploads": 1}
    _, c = contacts_import.parse(json.dumps(FB_NEWEST).encode(), "your_address_books.json")
    assert c == {"facebook_uploads": 1}


def test_instagram_synced_contacts():
    got, counts = contacts_import.parse(_zip({"connections/contacts/synced_contacts.json": IG_BOOK}),
                                        "instagram-david.zip")
    assert counts == {"instagram_uploads": 2}
    names = sorted(r["card"]["name"] for r in got["instagram_uploads"].values())
    assert names == ["Lionel Smith", "Mail Only"]


# --- what they do ------------------------------------------------------------------

def test_an_upload_makes_no_one_and_joins_a_messenger_person(book):
    names_before = {r["name"] for r in contacts.listing(limit=5000)["contacts"]}
    assert "Lionel Smith" in names_before                # Messenger's, on its own
    out = contacts.import_export(_zip({"your_address_books.json": FB_BOOK}), "facebook.zip")
    assert out["imported"] == {"facebook_uploads": 2}
    names = {r["name"] for r in contacts.listing(limit=5000)["contacts"]}
    assert "José Núñez" not in names and "Lionel Smith" not in names
    dad = _card_of(DAD)
    fb = [s for s in dad["sources"] if s["kind"] == "bridges"]
    assert fb and fb[0]["id"] == FB_LIONEL and fb[0]["why"] == "Facebook's uploaded contacts"
    assert len(names) == len(names_before) - 1          # Lionel folded into Dad, nobody new


def test_instagram_uploads_speak_for_instagram_only(book):
    _book(_ghost(IG_LIONEL, "Lionel Smith", "instagramgo", "Instagram"))
    contacts.import_export(_zip({"connections/contacts/synced_contacts.json": IG_BOOK}), "ig.zip")
    dad = _card_of(DAD)
    ids = {s["id"]: s.get("why") for s in dad["sources"] if s["kind"] == "bridges"}
    assert ids == {IG_LIONEL: "Instagram's uploaded contacts"}   # Messenger's waits for Facebook's


def test_two_uploaded_people_of_one_name_join_nobody(book):
    two = {"address_book_v2": {"address_book": [
        {"name": "Lionel Smith", "details": [{"contact_point": "0412 345 678"}]},
        {"name": "Lionel Smith", "details": [{"contact_point": "0400 999 000"}]}]}}
    contacts.import_export(json.dumps(two).encode(), "your_address_books.json")
    assert not any(s["kind"] == "bridges" for s in _card_of(DAD)["sources"])


def test_not_the_same_holds_against_an_upload(book):
    conn = contacts._connect()
    try:
        conn.execute("INSERT INTO merges (source, contact, kind, at) VALUES (?, ?, 'dismiss', 0)",
                     (FB_LIONEL, DAD))
    finally:
        conn.close()
    contacts.import_export(json.dumps(FB_BOOK).encode(), "your_address_books.json")
    assert not any(s["kind"] == "bridges" for s in _card_of(DAD)["sources"])


def test_a_newer_upload_replaces_the_last(book):
    contacts.import_export(json.dumps(FB_BOOK).encode(), "your_address_books.json")
    contacts.import_export(json.dumps(FB_OLD).encode(), "your_address_books.json")
    conn = contacts._connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM records WHERE kind='upload'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1
    assert not any(s["kind"] == "bridges" for s in _card_of(DAD)["sources"])


def test_the_export_links_name_the_uploads():
    nets = {x["network"]: x for x in contacts_exports.export_links()}
    assert {"facebook_uploads", "instagram_uploads"} <= set(nets)
    assert "Your address books" in nets["facebook_uploads"]["steps"]
    assert nets["instagram_uploads"]["url"] == contacts_exports.DYI_URL
