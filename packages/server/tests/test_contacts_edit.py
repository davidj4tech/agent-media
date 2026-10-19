"""Editing a contact from its card, written back to Google through a
stand-in phone (contacts_edit.py). Nothing here reaches the real one: the
adb command is a script in tmp_path that answers queries and logs writes."""

import json
import os
import sys

import pytest

from agent_media_server import auth, contacts, contacts_edit, devices
from test_contract import call, server, typed  # noqa: F401

NAME, PHONE, EMAIL, NOTE, GROUP = (contacts_edit.NAME, contacts_edit.PHONE,
                                   contacts_edit.EMAIL, contacts_edit.NOTE,
                                   contacts_edit.GROUP)


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
    reread = []
    monkeypatch.setattr(contacts_edit, "_later", lambda fn, *args: reread.append(args[0]))
    yield reread
    auth._REQ.__dict__.clear()
    contacts._reset_for_tests()


def phone(monkeypatch, tmp_path, rows=None, groups=None, google=True):
    """A stand-in phone: `rows` by mimetype, `[(id, data1)]`; `groups`
    `[(id, system_id, title)]`. Every script is logged."""
    log = tmp_path / "phone.log"
    rows = rows or {}
    groups = groups or []
    data = {m: "".join(f"Row: {i} _id={rid}, data1={d}\n" for i, (rid, d) in enumerate(rs))
            or "No result found.\n" for m, rs in rows.items()}
    grp = "".join(f"Row: {i} _id={g}, system_id={s}, title={t}\n"
                  for i, (g, s, t) in enumerate(groups)) or "No result found.\n"
    raw = "Row: 0 _id=12\n" if google else "No result found.\n"
    script = tmp_path / "adb"
    script.write_text(f"""#!{sys.executable}
import sys
s = sys.argv[-1]
open({str(log)!r}, "a").write(s + "\\n")
data = {data!r}
if "/raw_contacts" in s:
    print({raw!r}, end="")
elif "/groups" in s:
    print({grp!r}, end="")
elif s.startswith("content query"):
    for m, out in data.items():
        if m in s:
            print(out, end="")
            break
    else:
        print("No result found.")
""")
    script.chmod(0o755)
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", f"{script} shell")
    return log


def _writes(log):
    return [ln for ln in log.read_text().splitlines()
            if ln.startswith(("content insert", "content update", "content delete"))]


def _book(phones=("0412 345 678",), emails=("peter@example.test",), notes="old note",
          labels=("Family",), google_id="people/c123", accounts=("com.google",)):
    person = {"name": "Peter Ryan",
              "names": [{"display": "Peter Ryan", "given": "Peter", "family": "Ryan"}],
              "phones": [{"value": contacts.e164(p, "AU"), "raw": p, "type": "Mobile"}
                         for p in phones],
              "emails": [{"value": e, "type": "Home"} for e in emails],
              "addresses": [], "organizations": [], "notes": notes, "labels": list(labels)}
    recs = [("android", "lookup-1", person,
             {"raw": [12], "accounts": list(accounts), "version": 3})]
    if google_id:
        recs.append(("google", google_id, person, {"etag": "e1"}))
    conn = contacts._connect()
    try:
        for kind, sid, card, meta in recs:
            conn.execute("INSERT INTO records (kind, sid, card, meta) VALUES (?, ?, ?, ?)",
                         (kind, sid, json.dumps(card), json.dumps(meta)))
        contacts._rebuild(conn)
        (cid,) = [c for (c,) in conn.execute("SELECT id FROM contacts")]
    finally:
        conn.close()
    return cid


def test_the_card_says_editable_and_where_it_is_in_google():
    cid = _book()
    c = contacts.card(cid)
    assert c["edit"] is True
    assert c["google_url"] == "https://contacts.google.com/person/c123"


def test_no_google_entry_on_the_phone_is_not_editable():
    cid = _book(accounts=("com.whatsapp",), google_id="")
    c = contacts.card(cid)
    assert c["edit"] is False and c["google_url"] is None
    with pytest.raises(contacts_edit.NotEditable):
        contacts_edit.edit(cid, {"set": {"notes": "x"}})


def test_another_accounts_book_cannot_edit():
    key = auth.account_key_of("https://cms.test", "kat")
    with contacts.as_account(key):
        cid = _book()
        assert contacts.card(cid)["edit"] is False


def test_name_notes_phones_and_emails_are_written_through_the_phone(
        monkeypatch, tmp_path, isolated):
    cid = _book()
    log = phone(monkeypatch, tmp_path, rows={
        NAME: [("40", "Peter Ryan")],
        PHONE: [("41", "0412 345 678")],
        EMAIL: [("42", "peter@example.test")],
        NOTE: [("43", "old note")]})
    got = contacts_edit.edit(cid, {
        "base": {"name": "Peter Ryan", "phones": ["0412 345 678"],
                 "emails": ["peter@example.test"], "notes": "old note"},
        "set": {"name": "Pete Ryan", "phones": ["0412 345 678", "0400 111 222"],
                "emails": ["pete@example.test"], "notes": "met at the springs"}})
    assert got["ok"] and set(got["done"]) == {"name", "phones", "emails", "notes"}
    w = _writes(log)
    assert any(x.startswith("content update") and "_id=40" in x and "Pete Ryan" in x for x in w)
    assert any(x.startswith("content insert") and PHONE in x and "0400 111 222" in x for x in w)
    assert not any("_id=41" in x for x in w)                 # the kept number is left
    assert any(x.startswith("content delete") and "_id=42" in x for x in w)
    assert any(x.startswith("content insert") and "pete@example.test" in x for x in w)
    assert any(x.startswith("content update") and "_id=43" in x and "springs" in x for x in w)
    assert isolated == [""]                                    # the phone read again after


def test_labels_join_and_leave_googles_groups(monkeypatch, tmp_path):
    cid = _book(labels=("Family",))
    log = phone(monkeypatch, tmp_path, rows={GROUP: [("50", "7")]},
                groups=[("7", "NULL", "Family"), ("8", "NULL", "Work"),
                        ("9", "Contacts", "My Contacts")])
    got = contacts_edit.edit(cid, {"base": {"labels": ["Family"]},
                                   "set": {"labels": ["Work", "Book club"]}})
    assert got["skipped"] == ["Book club"]                    # no such group: not made
    w = _writes(log)
    assert any(x.startswith("content delete") and "_id=50" in x for x in w)
    assert any(x.startswith("content insert") and GROUP in x and "data1:i:8" in x for x in w)


def test_a_field_changed_since_is_a_conflict_and_nothing_is_written(monkeypatch, tmp_path):
    cid = _book(notes="changed in Google")
    log = phone(monkeypatch, tmp_path)
    with pytest.raises(contacts_edit.Conflict) as e:
        contacts_edit.edit(cid, {"base": {"notes": "what I saw"}, "set": {"notes": "mine"}})
    assert e.value.fields == {"notes": {"yours": "what I saw", "theirs": "changed in Google"}}
    assert not log.exists() or not _writes(log)


def test_nothing_changed_writes_nothing(monkeypatch, tmp_path):
    cid = _book()
    log = phone(monkeypatch, tmp_path)
    got = contacts_edit.edit(cid, {"base": {"notes": "old note"}, "set": {"notes": "old note"}})
    assert got == {"ok": True, "done": [], "skipped": []}
    assert not log.exists()


def test_bad_bodies_are_refused():
    cid = _book()
    for body in ({}, {"set": {}}, {"set": {"addresses": []}}, {"set": {"phones": "0412"}}):
        with pytest.raises(ValueError):
            contacts_edit.edit(cid, body)


def test_the_route(server, monkeypatch, tmp_path):
    cid = _book()
    phone(monkeypatch, tmp_path, rows={NOTE: [("43", "old note")]})
    code, _ = devices.mint_code("desk")
    tok = devices.redeem(code, "desk")["token"]
    H = {"Authorization": f"Bearer {tok}"}
    res, obj = call(server, "POST", f"/contacts/{cid}/edit", {"set": {"notes": "x"}})
    assert res.status in (401, 403)
    res, obj = call(server, "POST", f"/contacts/{cid}/edit",
                    {"base": {"notes": "old note"}, "set": {"notes": "new"}}, H)
    assert res.status == 200 and obj["done"] == ["notes"]
    res, obj = call(server, "POST", f"/contacts/{cid}/edit",
                    {"base": {"notes": "stale"}, "set": {"notes": "new"}}, H)
    assert res.status == 409 and obj["conflict"]["notes"]["theirs"] == "old note"
    res, obj = call(server, "POST", f"/contacts/{cid}/edit", {"set": {"colour": "x"}}, H)
    assert res.status == 400
