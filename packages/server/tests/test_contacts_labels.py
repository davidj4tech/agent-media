"""Labels (§6.25): the phone's contact groups (Google's labels, synced by
Android) and a Google book's `contactGroups`. The phone's answer is a made-up
`content query` print, never the real phone; Google is never called (its
requests are faked); HOME, the config dir and the state dir are throwaway."""

from __future__ import annotations

import json
import os

import pytest

from agent_media_server import contacts
from agent_media_server import contacts_android as ca
from test_contract import AUTH, call, server, signed_in, typed  # noqa: F401


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

    def nowhere(*a, **k):
        raise AssertionError(f"a real Google request: {a[:2]}")
    monkeypatch.setattr(contacts, "_request", nowhere)
    yield
    contacts._reset_for_tests()


# --- the phone ------------------------------------------------------------------

def _content(rows, cols):
    if not rows:
        return "No result found.\n"
    return "".join(
        f"Row: {i} " + ", ".join(f"{c}={'NULL' if r.get(c) is None else r[c]}" for c in cols) + "\n"
        for i, r in enumerate(rows))


def _output(nonce, raw, data, groups):
    m = f"@@AM-{nonce}@@"
    out = [f"{m} raw\n", _content(raw, ca.RAW_COLS), f"{m} data\n", _content(data, ca.DATA_COLS)]
    for c in ca.FREE_COLS:
        out += [f"{m} {c}\n", _content([{"_id": d["_id"], c: d.get(c)} for d in data], ("_id", c))]
    if groups is not None:
        out += [f"{m} groups\n", _content(groups, ca.GROUP_COLS), f"{m} group_title\n",
                _content([{"_id": g["_id"], "title": g.get("title")} for g in groups],
                         ("_id", "title"))]
    out.append(f"{m} end\n")
    return "".join(out)


def _d(i, contact, raw, lookup, kind, d1, display="Lionel Smith"):
    return {"_id": str(i), "contact_id": contact, "raw_contact_id": raw, "lookup": lookup,
            "mimetype": ca.MIME + kind, "data_version": "1", "display_name": display,
            "data1": d1, "data2": None, "data3": None, "data4": None}


RAW = [{"_id": "11", "contact_id": "1", "account_type": "com.google", "version": "1"},
       {"_id": "21", "contact_id": "2", "account_type": "com.google", "version": "1"}]
GROUPS = [
    {"_id": "1", "system_id": "Contacts", "account_type": "com.google", "title": "My Contacts"},
    {"_id": "2", "system_id": "Family", "account_type": "com.google", "title": "Family"},
    {"_id": "3", "system_id": "Starred in Android", "account_type": "com.google", "title": "Starred"},
    {"_id": "4", "system_id": None, "account_type": "com.google", "title": "Book club, Tuesdays"},
    {"_id": "5", "system_id": None, "account_type": "com.whatsapp", "title": "WA group"},
    {"_id": "6", "system_id": None, "account_type": "com.google", "title": None},
]
DATA = [
    _d(1, "1", "11", "L1", "name", "Lionel Smith"),
    _d(2, "1", "11", "L1", "group_membership", "1"),
    _d(3, "1", "11", "L1", "group_membership", "2"),
    _d(4, "1", "11", "L1", "group_membership", "3"),           # starred: left out
    _d(5, "1", "11", "L1", "group_membership", "4"),
    _d(6, "1", "11", "L1", "group_membership", "5"),           # WhatsApp's: left out
    _d(7, "1", "11", "L1", "group_membership", "6"),           # no title: left out
    _d(8, "2", "21", "M2", "name", "Mel Jones", "Mel Jones"),
    _d(9, "2", "21", "M2", "group_membership", "1", "Mel Jones"),
]


@pytest.fixture
def phone(monkeypatch):
    state = {"groups": GROUPS}

    def run(timeout=ca.TIMEOUT_S):
        return "n", _output("n", RAW, DATA, state["groups"])

    monkeypatch.setattr(ca, "run", run)
    monkeypatch.setattr(ca, "available", lambda: True)
    return state


def test_the_script_reads_the_groups_too():
    s = ca.script("abc")
    assert f"--uri {ca.URI}/groups --projection _id:system_id:account_type --where 'deleted=0'" in s
    assert f"--uri {ca.URI}/groups --projection _id:title" in s
    assert "'vnd.android.cursor.item/group_membership'" in s
    assert s.index("@@AM-abc@@ group_title") < s.index("@@AM-abc@@ end")


def test_the_phones_labels_by_name(phone):
    contacts.sync_android()
    lionel = contacts.card(contacts.contact_id("android", "L1"))
    assert lionel["labels"] == ["Book club, Tuesdays", "Family", "My Contacts"]
    mel = contacts.card(contacts.contact_id("android", "M2"))
    assert mel["labels"] == ["My Contacts"]


def test_an_answer_without_groups_is_no_labels(phone):
    phone["groups"] = None                       # an older script's answer: no sections
    contacts.sync_android()
    assert contacts.card(contacts.contact_id("android", "L1"))["labels"] == []


def test_filter_and_list_by_label(phone):
    contacts.sync_android()
    assert [c["name"] for c in contacts.listing(label="my contacts")["contacts"]] == [
        "Lionel Smith", "Mel Jones"]
    assert [c["name"] for c in contacts.listing(label="Family")["contacts"]] == ["Lionel Smith"]
    assert contacts.listing(label="Family")["contacts"][0]["labels"] == [
        "Book club, Tuesdays", "Family", "My Contacts"]
    assert contacts.listing(label="nobody's")["contacts"] == []
    assert contacts.labels() == {"labels": [{"name": "Book club, Tuesdays", "count": 1},
                                            {"name": "Family", "count": 1},
                                            {"name": "My Contacts", "count": 2}]}


def test_several_labels_any_or_all(phone):
    contacts.sync_android()
    names = lambda got: [c["name"] for c in got["contacts"]]  # noqa: E731
    assert names(contacts.listing(label=["Family", "My Contacts"])) == ["Lionel Smith", "Mel Jones"]
    assert names(contacts.listing(label=["Family", "My Contacts"], mode="any")) == [
        "Lionel Smith", "Mel Jones"]
    assert names(contacts.listing(label=["family", "MY CONTACTS"], mode="all")) == ["Lionel Smith"]
    assert names(contacts.listing(label=["Family", "nobody's"], mode="all")) == []
    assert names(contacts.listing(label=["Family", "nobody's"])) == ["Lionel Smith"]
    assert names(contacts.listing(label=["Book club, Tuesdays"])) == ["Lionel Smith"]
    assert names(contacts.listing(label=["", "Family"], mode="all")) == ["Lionel Smith"]
    assert len(contacts.listing(label=[])["contacts"]) == 2
    assert names(contacts.listing("mel", label=["My Contacts", "Family"])) == ["Mel Jones"]


def test_the_routes(server, signed_in, phone):
    contacts.sync_android()
    res, obj = call(server, "GET", "/contacts/labels", headers=AUTH)
    assert res.status == 200 and obj["ok"] and [x["name"] for x in obj["labels"]] == [
        "Book club, Tuesdays", "Family", "My Contacts"]
    assert res.getheader("Access-Control-Allow-Origin") == "*"
    res, obj = call(server, "GET", "/contacts?label=Family", headers=AUTH)
    assert res.status == 200 and [c["name"] for c in obj["contacts"]] == ["Lionel Smith"]
    res, obj = call(server, "GET", "/contacts?label=Family&label=My%20Contacts", headers=AUTH)
    assert res.status == 200 and [c["name"] for c in obj["contacts"]] == [
        "Lionel Smith", "Mel Jones"]
    res, obj = call(server, "GET", "/contacts?label=Family&label=My%20Contacts&labels_mode=all",
                    headers=AUTH)
    assert res.status == 200 and [c["name"] for c in obj["contacts"]] == ["Lionel Smith"]
    res, obj = call(server, "GET", "/contacts?label=Book%20club%2C%20Tuesdays&labels_mode=all",
                    headers=AUTH)
    assert [c["name"] for c in obj["contacts"]] == ["Lionel Smith"]


def test_the_routes_are_gated(server, monkeypatch):
    from agent_media_server import auth_abs

    monkeypatch.setattr(auth_abs, "abs_urls", lambda: ["http://abs"])
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: (None, 401))
    res, _ = call(server, "GET", "/contacts/labels")
    assert res.status in (401, 403)


# --- Google ---------------------------------------------------------------------

def test_googles_memberships_by_name():
    person = {"names": [{"displayName": "Lionel Smith"}], "memberships": [
        {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/myContacts"}},
        {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/starred"}},
        {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/friends"}},
        {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/abc"}},
        {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/gone"}},
        {"domainMembership": {"inViewerDomain": True}}]}
    card = contacts.card_of(person, "AU", {"contactGroups/abc": "Book club"})
    assert card["labels"] == ["Book club", "Friends", "My Contacts"]
    assert "labels" not in contacts.card_of({"names": []}, "AU")


def test_new_fields_read_google_whole_again(monkeypatch):
    contacts._save_token({"refresh_token": "r", "obtained_at": 1})
    calls = []

    def fetch(token):
        calls.append(token)
        return ([{"resourceName": "people/c1", "etag": "e",
                  "names": [{"displayName": "Lionel Smith"}],
                  "memberships": [{"contactGroupMembership": {
                      "contactGroupResourceName": "contactGroups/abc"}}]}], "next-token")

    monkeypatch.setattr(contacts, "_fetch", fetch)
    monkeypatch.setattr(contacts, "_groups", lambda: {"contactGroups/abc": "Book club"})
    conn = contacts._connect()
    contacts._set_meta(conn, "google_sync_token", "made-before-labels")
    conn.close()
    assert contacts.sync_google()["full"] is True and calls == [None]
    cid = contacts.contact_id("google", "people/c1")
    assert contacts.card(cid)["labels"] == ["Book club"]
    contacts.sync_google()
    assert calls == [None, "next-token"]                 # the fields now match: changes only


def test_labels_from_both_are_one_list():
    a = {"name": "L", "labels": ["Family", "book club"]}
    b = {"name": "L", "labels": ["Book Club", "Work"]}
    assert contacts._merge(a, b)["labels"] == ["book club", "Family", "Work"]
