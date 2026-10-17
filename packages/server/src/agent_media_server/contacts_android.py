"""The phone's contacts, read over adb (contacts.py's "android" source).

David, 17 Oct 2026: "I'm pretty unfamiliar with Google cloud. can you run it
with ADB?" The phone's contacts provider already holds his Google Contacts
(the com.google raw contacts, synced by Android) merged with WhatsApp's into
one aggregate per person, so it is read as shell uid with `content query`
through `agent-phone-adb shell` (`MEDIA_CONTACTS_ADB` replaces that command).
adb only answers while the phone is on Wi-Fi: a read that fails raises
`Unreachable`, and the store keeps its last good copy.

**Reading `content query` safely.** It prints `Row: N col=value, col=value`
with nothing escaped: a note can hold ", data2=" or a newline. So:

* every free-text column (`display_name`, `data1`…`data4`) is read in a query
  of its own, `_id:<col>`, where it is the LAST column — its value is the
  rest of the row, however many commas or `=` it holds;
* a line is a new row only when it is `Row: <the next index> `; any other
  line continues the value before it (a newline in a note or an address);
* the structural columns (ids, `lookup`, `mimetype`, versions, the account
  type) hold no free text, and are split by their names in projection order;
* the queries run as one shell script, each section opened by a marker with
  a fresh random nonce, and a missing `end` marker is a failed read;
* `NULL` is None (Android prints a null as `NULL`).

The `data` URI has no `account_type` or `deleted` (its projection is strict),
so those come from `raw_contacts`: a data row whose raw contact is deleted,
or not listed, is dropped.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess

URI = "content://com.android.contacts"
MIME = "vnd.android.cursor.item/"
#: The kinds of row read: everything else (photos, groups, WhatsApp's own
#: action rows) is left on the phone.
KINDS = ("phone_v2", "email_v2", "postal-address_v2", "note", "name", "organization")
RAW_COLS = ("_id", "contact_id", "account_type", "version")
DATA_COLS = ("_id", "contact_id", "raw_contact_id", "lookup", "mimetype", "data_version")
FREE_COLS = ("display_name", "data1", "data2", "data3", "data4")
#: A whole read, at most (ssh, then adb, then ~6 queries of a few thousand rows).
TIMEOUT_S = 180.0

_PHONE_TYPES = {"1": "Home", "2": "Mobile", "3": "Work", "4": "Work fax", "5": "Home fax",
                "6": "Pager", "7": "Other", "8": "Callback", "9": "Car", "10": "Company main",
                "12": "Main", "17": "Work mobile"}
_EMAIL_TYPES = {"1": "Home", "2": "Work", "3": "Other", "4": "Mobile"}
_POSTAL_TYPES = {"1": "Home", "2": "Work", "3": "Other"}


class Unreachable(Exception):
    """The phone did not answer (off Wi-Fi, adb down) or answered with an
    error: nothing was read."""


def command() -> list[str]:
    return shlex.split(os.environ.get("MEDIA_CONTACTS_ADB") or "agent-phone-adb shell")


def available() -> bool:
    """Whether this host has the command at all (a host without the phone
    tool never tries)."""
    cmd = command()
    if not cmd:
        return False
    if os.path.sep in cmd[0]:
        return os.access(cmd[0], os.X_OK)
    return shutil.which(cmd[0]) is not None


def script(nonce: str) -> str:
    """The one shell script run on the phone: a marker, then a query, per
    section."""
    kinds = ",".join(f"'{MIME}{k}'" for k in KINDS)
    where = f'"mimetype IN ({kinds})"'
    m = f"@@AM-{nonce}@@"
    parts = [f"echo '{m} raw'",
             f"content query --uri {URI}/raw_contacts --projection {':'.join(RAW_COLS)} "
             f"--where 'deleted=0' --sort _id",
             f"echo '{m} data'",
             f"content query --uri {URI}/data --projection {':'.join(DATA_COLS)} "
             f"--where {where} --sort _id"]
    for col in FREE_COLS:
        parts += [f"echo '{m} {col}'",
                  f"content query --uri {URI}/data --projection _id:{col} "
                  f"--where {where} --sort _id"]
    parts.append(f"echo '{m} end'")
    return "; ".join(parts)


def run(timeout: float = TIMEOUT_S) -> tuple[str, str]:
    """`(nonce, output)` of the script on the phone."""
    nonce = secrets.token_hex(8)
    try:
        got = subprocess.run(command() + [script(nonce)], capture_output=True, text=True,
                             timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise Unreachable(f"the phone did not answer in {int(timeout)} s") from None
    except OSError as e:
        raise Unreachable(f"could not run {command()[0]} ({e})") from None
    if got.returncode != 0:
        last = (got.stderr or got.stdout or "").strip().splitlines()[-1:] or [""]
        raise Unreachable(f"{command()[0]} exited {got.returncode}: {last[0][:200]}")
    return nonce, got.stdout


# --- parsing --------------------------------------------------------------------

def sections(output: str, nonce: str) -> dict[str, str]:
    """The output cut at its markers; Unreachable when it was cut short."""
    m = re.escape(f"@@AM-{nonce}@@")
    out: dict[str, list[str]] = {}
    cur: str | None = None
    for line in output.split("\n"):
        hit = re.fullmatch(m + r" (\S+)\r?", line)
        if hit:
            cur = hit.group(1)
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    if "end" not in out:
        raise Unreachable("the read was cut short (no end marker)")
    return {k: "\n".join(v) for k, v in out.items() if k != "end"}


def rows(text: str) -> list[str]:
    """Each row's text after `Row: N `, newlines inside a value kept. A line
    is a new row only when it is the next index."""
    text = text.replace("\r\n", "\n")
    head = text.strip()
    if head and head != "No result found." and not head.startswith("Row: 0 "):
        # "Error while accessing provider…", a Java exception, adb's own
        # complaint: not rows.
        raise Unreachable("the contacts provider refused the query: "
                          + head.splitlines()[0][:200])
    out: list[str] = []
    for line in text.split("\n"):
        want = f"Row: {len(out)} "
        if line.startswith(want):
            out.append(line[len(want):])
        elif out:
            out[-1] += "\n" + line
    # The output's own last newline is not part of the last value.
    if out:
        out[-1] = out[-1].rstrip("\n")
    return out


def _null(v: str | None) -> str | None:
    return None if v is None or v == "NULL" else v


def split(row: str, cols: tuple[str, ...]) -> dict[str, str | None]:
    """A row by its columns, in projection order: each value runs to `, <the
    next column>=`, and the last to the end of the row."""
    first = f"{cols[0]}="
    if not row.startswith(first):
        raise ValueError(f"a row that does not start with {first!r}")
    out: dict[str, str | None] = {}
    pos = len(first)
    for i, col in enumerate(cols):
        if i + 1 < len(cols):
            sep = f", {cols[i + 1]}="
            end = row.find(sep, pos)
            if end < 0:
                raise ValueError(f"no {cols[i + 1]} in a row")
            out[col] = _null(row[pos:end])
            pos = end + len(sep)
        else:
            out[col] = _null(row[pos:])
    return out


def parse(output: str, nonce: str) -> dict:
    """The phone's answer as `{"raw": {id: {...}}, "data": {id: {...}}}`,
    each data row with its free-text columns joined in."""
    sec = sections(output, nonce)
    for need in ("raw", "data", *FREE_COLS):
        if need not in sec:
            raise Unreachable(f"the read has no {need} section")
    raw = {}
    for r in rows(sec["raw"]):
        d = split(r, RAW_COLS)
        raw[d["_id"]] = d
    data = {}
    for r in rows(sec["data"]):
        d = split(r, DATA_COLS)
        data[d["_id"]] = d
    for col in FREE_COLS:
        for r in rows(sec[col]):
            d = split(r, ("_id", col))
            if d["_id"] in data:
                data[d["_id"]][col] = d[col]
    return {"raw": raw, "data": data}


# --- into records ---------------------------------------------------------------

def _label(types: dict, t: str | None, custom: str | None) -> str:
    if t == "0":
        return custom or ""
    return types.get(t or "", "")


def records(parsed: dict, default_region: str = "AU") -> dict[str, dict]:
    """One record per contact, keyed by its lookup key: `{"card": <the card
    as contacts.py keeps it>, "meta": {"raw", "accounts", "version"}}`."""
    from .contacts import e164

    raw = parsed["raw"]
    by_contact: dict[str, list[dict]] = {}
    for d in parsed["data"].values():
        r = raw.get(d.get("raw_contact_id") or "")
        if r is None:
            continue                   # a deleted raw contact, or one not listed
        by_contact.setdefault(d.get("contact_id") or "", []).append(d)
    out: dict[str, dict] = {}
    for cid, ds in by_contact.items():
        if not cid:
            continue
        ds.sort(key=lambda d: int(d["_id"]) if (d["_id"] or "").isdigit() else 0)
        lookups = [d["lookup"] for d in ds if d.get("lookup")]
        if not lookups:
            continue
        lookup = max(set(lookups), key=lookups.count)
        names, phones, emails, addresses, notes, orgs = [], [], [], [], [], []
        seen: set = set()

        def once(key) -> bool:
            if key in seen:
                return False
            seen.add(key)
            return True

        display = ""
        for d in ds:
            kind = (d.get("mimetype") or "").removeprefix(MIME)
            d1, d2, d3, d4 = (d.get(c) for c in ("data1", "data2", "data3", "data4"))
            display = display or (d.get("display_name") or "")
            if kind == "name":
                n = {"display": d1 or "", "given": d2 or "", "family": d3 or ""}
                if any(n.values()) and once(("n", n["display"], n["given"], n["family"])):
                    names.append(n)
            elif kind == "phone_v2" and d1:
                num = e164(d4, default_region) or e164(d1, default_region)
                if once(("p", num or re.sub(r"\s", "", d1))):
                    phones.append({"value": num, "raw": d1,
                                   "type": _label(_PHONE_TYPES, d2, d3)})
            elif kind == "email_v2" and d1:
                if once(("e", d1.strip().lower())):
                    emails.append({"value": d1.strip(), "type": _label(_EMAIL_TYPES, d2, d3)})
            elif kind == "postal-address_v2" and d1:
                if once(("a", " ".join(d1.split()).lower())):
                    addresses.append({"formatted": d1, "type": _label(_POSTAL_TYPES, d2, d3)})
            elif kind == "note" and d1 and d1.strip():
                if once(("t", d1.strip())):
                    notes.append(d1.strip())
            elif kind == "organization" and (d1 or d4):
                if once(("o", d1 or "", d4 or "")):
                    orgs.append({"company": d1 or "", "title": d4 or ""})
        raw_ids = sorted({d["raw_contact_id"] for d in ds}, key=lambda x: int(x) if x.isdigit() else 0)
        accounts = sorted({raw[i].get("account_type") or "" for i in raw_ids} - {""})
        version = sum(int(raw[i].get("version") or 0) for i in raw_ids
                      if (raw[i].get("version") or "").isdigit())
        name = display or next((n["display"] for n in names if n["display"]), "") \
            or next((e["value"] for e in emails), "") \
            or next((p["raw"] for p in phones), "") or "(no name)"
        out[lookup] = {"card": {"name": name, "names": names, "phones": phones,
                                "emails": emails, "addresses": addresses,
                                "notes": "\n\n".join(notes), "organizations": orgs},
                       "meta": {"raw": [int(i) if i.isdigit() else i for i in raw_ids],
                                "accounts": accounts, "version": version}}
    return out


def read(default_region: str = "AU") -> tuple[dict[str, dict], str]:
    """Every contact on the phone, and a hash of what was read (unchanged
    hash: nothing to write)."""
    nonce, output = run()
    try:
        recs = records(parse(output, nonce), default_region)
    except ValueError as e:
        raise Unreachable(f"the phone's answer could not be read ({e})") from None
    digest = hashlib.sha256(json.dumps(recs, sort_keys=True).encode()).hexdigest()
    return recs, digest
