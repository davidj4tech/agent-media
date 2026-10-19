"""Editing a contact from its card, written back to Google (contacts.py's
step 4, its second part).

David, 20 Oct 2026: an Edit button on the card for name, phones, emails,
notes and labels, written back to Google, with "Open in Google Contacts"
beside it. The write goes through the phone, as the read and the Join's
links do (contacts_writeback.py): `content` updates, inserts and deletes on
the person's `com.google` raw contact, as shell uid, which marks it dirty,
and Android syncs it to Google. No Cloud project; the People API sign-in
stays read-only.

* **What changes.** `name` updates the structured-name row's display name
  (the provider splits it into given and family); `phones` and `emails` are
  the whole list wanted: a value no longer there has its row deleted, a new
  one is inserted (a mobile number, an "other" address), the rest are left
  as they are, labels and all; `notes` updates the note row or adds one;
  `labels` adds and removes Google group memberships, by the label's name
  among the phone's `com.google` groups (a label Google has no group for is
  not made: said back as `skipped`).
* **Conflicts.** The app sends what it showed (`base`) beside what it wants
  (`set`). A field whose value on the card now is not what the app showed
  was changed elsewhere since (in Google Contacts, by a sync): 409 with
  both, `{field: {"yours": …, "theirs": …}}`, and nothing is written. (The
  People API's own etag guards a People API write; this write goes through
  the phone, so the card is the guard.)
* **After.** The phone is read again in a thread, so the card shows what
  Google now holds within moments.
* The owner's book only, and only a person with a Google entry on the phone
  (`editable`); off with `MEDIA_CONTACTS_WRITEBACK=0`, as the Join's links.
"""

from __future__ import annotations

import json
import re
import shlex
import threading

from . import contacts, contacts_android, contacts_writeback as wb

URI = contacts_android.URI
NAME = "vnd.android.cursor.item/name"
PHONE = "vnd.android.cursor.item/phone_v2"
EMAIL = "vnd.android.cursor.item/email_v2"
NOTE = "vnd.android.cursor.item/note"
GROUP = "vnd.android.cursor.item/group_membership"
#: Phone.TYPE_MOBILE, Email.TYPE_OTHER.
PHONE_MOBILE = 2
EMAIL_OTHER = 3
FIELDS = ("name", "phones", "emails", "notes", "labels")


class Conflict(Exception):
    def __init__(self, fields: dict):
        super().__init__("changed since")
        self.fields = fields


class NotEditable(Exception):
    pass


def editable(card: dict) -> bool:
    """Whether a card can be edited here: the owner's book, write-back on,
    and a Google entry on the phone behind it."""
    return contacts.is_owner() and wb.enabled() and bool(wb._raw_ids(card))


def google_url(card: dict) -> str:
    """The person in Google Contacts, from a Google source's resource name
    (`people/c123…`), or ""."""
    for s in card.get("sources") or []:
        if s.get("kind") == "google":
            m = re.fullmatch(r"people/(c\d+)", s.get("id") or "")
            if m:
                return f"https://contacts.google.com/person/{m.group(1)}"
    return ""


def _now(card: dict, field: str):
    """A field as the card shows it, in the form `set` takes."""
    if field == "name":
        return card.get("name") or ""
    if field == "phones":
        return [p.get("raw") or p.get("value") or "" for p in card.get("phones") or []]
    if field == "emails":
        return [e.get("value") or e.get("address") or "" for e in card.get("emails") or []
                if not e.get("derived")]
    if field == "notes":
        return card.get("notes") or ""
    return list(card.get("labels") or [])


def _same(field: str, a, b) -> bool:
    if field == "phones":
        reg = contacts.region()
        norm = lambda xs: sorted(contacts.e164(x, reg) or re.sub(r"\D", "", x) for x in xs)  # noqa: E731
        return norm(a or []) == norm(b or [])
    if field in ("emails", "labels"):
        return sorted(x.strip().lower() for x in a or []) == \
            sorted(x.strip().lower() for x in b or [])
    return (a or "").strip() == (b or "").strip()


def _clean(field: str, v):
    if field in ("phones", "emails", "labels"):
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError(f"{field} must be a list of strings")
        return [x.strip() for x in v if x.strip()][:50]
    if not isinstance(v, str):
        raise ValueError(f"{field} must be a string")
    return v.strip()[:4000 if field == "notes" else 300]


def _rows(raw: str, mimetype: str, cols: str = "_id:data1") -> list[dict]:
    where = f"raw_contact_id={raw} AND mimetype='{mimetype}'"
    out = wb._shell(f"content query --uri {URI}/data --projection {cols} --where \"{where}\"")
    rows = []
    for line in out.splitlines():
        m = re.match(r"^Row: \d+ _id=(\d+)(?:, data1=(.*))?$", line)
        if m:
            rows.append({"id": m.group(1), "data1": m.group(2) or ""})
    return rows


def _binds(binds: list[str]) -> str:
    return " ".join(f"--bind {shlex.quote(b)}" for b in binds)


def _insert(raw: str, mimetype: str, binds: list[str]) -> None:
    wb._shell(f"content insert --uri {URI}/data "
              + _binds([f"raw_contact_id:i:{raw}", f"mimetype:s:{mimetype}", *binds]))


def _update(row_id: str, binds: list[str]) -> None:
    wb._shell(f"content update --uri {URI}/data {_binds(binds)} --where \"_id={row_id}\"")


def _delete(row_id: str) -> None:
    wb._shell(f"content delete --uri {URI}/data --where \"_id={row_id}\"")


def _groups() -> dict[str, str]:
    """The phone's Google groups by folded title: `{title: _id}` ("My
    Contacts" is the system group `Contacts`)."""
    where = "deleted=0 AND account_type='com.google'"
    out = wb._shell(f"content query --uri {URI}/groups --projection _id:system_id:title "
                    f"--where \"{where}\"")
    got: dict[str, str] = {}
    for line in out.splitlines():
        m = re.match(r"^Row: \d+ _id=(\d+), system_id=(.*?), title=(.*)$", line)
        if not m:
            continue
        gid, system, title = m.group(1), m.group(2), m.group(3)
        name = "My Contacts" if system == "Contacts" else title
        if name and name != "NULL":
            got.setdefault(contacts.fold(name), gid)
    return got


def _write(raw: str, card: dict, want: dict) -> dict:
    done, skipped = [], []
    if "name" in want and want["name"]:
        rows = _rows(raw, NAME)
        b = [f"data1:s:{want['name']}", "data2:n:", "data3:n:", "data4:n:", "data5:n:",
             "data6:n:"]
        if rows:
            _update(rows[0]["id"], b)
        else:
            _insert(raw, NAME, [f"data1:s:{want['name']}"])
        done.append("name")
    if "phones" in want:
        reg = contacts.region()
        key = lambda x: contacts.e164(x, reg) or re.sub(r"\D", "", x)  # noqa: E731
        rows = _rows(raw, PHONE)
        wanted = {key(x): x for x in want["phones"]}
        have = {key(r["data1"]): r for r in rows}
        for k, r in have.items():
            if k not in wanted:
                _delete(r["id"])
        for k, x in wanted.items():
            if k not in have:
                _insert(raw, PHONE, [f"data1:s:{x}", f"data2:i:{PHONE_MOBILE}"])
        done.append("phones")
    if "emails" in want:
        rows = _rows(raw, EMAIL)
        wanted = {x.lower(): x for x in want["emails"]}
        have = {r["data1"].strip().lower(): r for r in rows}
        for k, r in have.items():
            if k not in wanted:
                _delete(r["id"])
        for k, x in wanted.items():
            if k not in have:
                _insert(raw, EMAIL, [f"data1:s:{x}", f"data2:i:{EMAIL_OTHER}"])
        done.append("emails")
    if "notes" in want:
        rows = _rows(raw, NOTE)
        if rows:
            if want["notes"]:
                _update(rows[0]["id"], [f"data1:s:{want['notes']}"])
            else:
                _delete(rows[0]["id"])
        elif want["notes"]:
            _insert(raw, NOTE, [f"data1:s:{want['notes']}"])
        done.append("notes")
    if "labels" in want:
        groups = _groups()
        rows = _rows(raw, GROUP)
        member = {r["data1"]: r for r in rows}
        wanted: set[str] = set()
        for label in want["labels"]:
            gid = groups.get(contacts.fold(label))
            if gid is None:
                skipped.append(label)
            else:
                wanted.add(gid)
        mine = {contacts.fold(x) for x in _now(card, "labels")}
        for gid, r in member.items():
            # Only a membership the card shows as a label is taken away.
            title = next((t for t, g in groups.items() if g == gid), None)
            if gid not in wanted and title in mine:
                _delete(r["id"])
        for gid in wanted - set(member):
            _insert(raw, GROUP, [f"data1:i:{gid}"])
        done.append("labels")
    return {"done": done, "skipped": skipped}


def edit(cid: str, body: dict) -> dict:
    """`PATCH /contacts/<id>` `{base: {field: as shown}, set: {field: wanted}}`:
    `{ok, done, skipped}`. Raises NotEditable, Conflict, ValueError, or the
    phone's Unreachable / WriteError."""
    if not isinstance(body, dict) or not isinstance(body.get("set"), dict) or not body["set"]:
        raise ValueError("set the fields to change")
    unknown = set(body["set"]) - set(FIELDS)
    if unknown:
        raise ValueError(f"cannot edit {', '.join(sorted(unknown))}")
    base = body.get("base") if isinstance(body.get("base"), dict) else {}
    conn = contacts._connect(create=False)
    if conn is None:
        raise NotEditable("no such contact")
    try:
        row = conn.execute("SELECT card, name FROM contacts WHERE id=?", (cid,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise NotEditable("no such contact")
    card = json.loads(row[0])
    card.setdefault("name", row[1])
    if not editable(card):
        raise NotEditable("only a person with a Google entry on the phone can be edited here")
    want = {f: _clean(f, v) for f, v in body["set"].items()}
    clash = {}
    for f in want:
        if f in base and not _same(f, base[f], _now(card, f)):
            clash[f] = {"yours": base[f], "theirs": _now(card, f)}
    if clash:
        raise Conflict(clash)
    want = {f: v for f, v in want.items() if not _same(f, v, _now(card, f))}
    if not want:
        return {"ok": True, "done": [], "skipped": []}
    raw = wb._google_raw(wb._raw_ids(card))
    if raw is None:
        raise NotEditable("no Google entry on the phone any more")
    got = _write(raw, card, want)
    _later(_reread, contacts.account())
    return {"ok": True, **got}


def _later(fn, *args) -> None:
    threading.Thread(target=fn, args=args, name="contacts-edit-reread", daemon=True).start()


def _reread(key: str) -> None:
    try:
        with contacts.as_account(key):
            contacts.sync_android()
    except Exception as e:  # noqa: BLE001 — the next sync loop reads it anyway
        contacts.log.info("contacts: the read after an edit failed: %s", e)
