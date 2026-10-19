"""People from a network's own data export (David, 19 Oct 2026: "a full list
of my bridged contacts").

The bridges only know who is in a chat; the lists themselves come from the
networks' downloads, which is their own, sanctioned way out (pulling them
through a bridge would be an unofficial client scraping):

* **Facebook**, "Download your information", JSON: `your_friends.json`
  (`connections/friends/`, older `friends_and_followers/friends.json` or
  `friends/friends.json`), `{"friends_v2": [{"name", "timestamp"}]}` or the
  older `{"friends": […]}`, and the newest exports' `label_values` rows.
  Facebook writes each UTF-8 byte as its own `\\u00xx` escape ("JosÃ©"):
  undone here.
* **Instagram**, "Download your information", JSON:
  `connections/followers_and_following/followers_1.json` (`followers_2…`)
  and `following.json`: rows of `string_list_data [{href, value: username,
  timestamp}]` (the newer following rows carry the username as `title`).

`parse(data, name)` takes a .zip of the export (or of part of it) or one of
those JSON files and answers `{export: {sid: {card, meta}}}`, ready for
contacts.import_people. Each person is one record (someone both a follower
and followed is one, with both labels); its id is stable across exports, so
a newer one replaces the older and David's Joins hold. Nothing here logs a
name.

**Uploaded contacts** (David, 19 Oct 2026): the phone address books David
once let Facebook, Messenger or Instagram sync, from the same downloads —
Facebook's `your_address_books.json` (`{"address_book_v2": {"address_book":
[{"name", "details": [{"contact_point"}]}]}}`, the older `address_book`,
and the newest exports' `label_values` rows) and Instagram's
`synced_contacts.json` (`{"contacts_contact_info": [{"string_map_data":
{"First name", "Surname", "Contact information"}}]}`). They are his own
address book again, so they make nobody: under `facebook_uploads` /
`instagram_uploads` they are kept as evidence (contacts.py, the `upload`
records), and a network's person whose name is an uploaded entry's joins
the one contact holding that entry's number or address.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import zipfile
from urllib.parse import urlparse

#: The labels each list gives its people.
LABELS = {("facebook", "friends"): "Facebook friends",
          ("instagram", "followers"): "Instagram followers",
          ("instagram", "following"): "Instagram following"}
#: The most an uncompressed export file may be read to (a friends list of
#: 5000 is about 400 KB; this is room for the largest with margin).
MAX_FILE = 32 * 1024 * 1024
#: The most files of a zip looked at.
MAX_MEMBERS = 20000


class NotAnExport(ValueError):
    """Neither a Facebook friends list nor an Instagram followers or
    following list, in JSON (an HTML export says so)."""


def fix_text(s: str) -> str:
    """Facebook's escaped UTF-8 ("JosÃ©" → "José"); anything else as it is."""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _kind_of(path: str) -> tuple[str, str] | None:
    """Which list a file of an export is, by its path; None for the rest
    (removed friends, requests, close friends…)."""
    p = path.replace("\\", "/").lower()
    base = p.rsplit("/", 1)[-1]
    if not base.endswith(".json"):
        return None
    if base in ("your_address_books.json", "address_books.json"):
        return ("facebook", "uploads")
    if base in ("synced_contacts.json", "contacts_synced.json") or (
            "/contacts/" in "/" + p and base.startswith(("synced_contact", "contacts_synced"))):
        return ("instagram", "uploads")
    if base == "your_friends.json" or (base == "friends.json" and (
            "/friends/" in "/" + p or "friends_and_followers/" in p)):
        return ("facebook", "friends")
    if re.fullmatch(r"followers(_\d+)?\.json", base) and "followers_and_following" in p:
        return ("instagram", "followers")
    if base == "following.json" and "followers_and_following" in p:
        return ("instagram", "following")
    return None


def _guess(obj, name: str) -> tuple[str, str] | None:
    """A lone JSON file's list, by what it holds (its name helps)."""
    if isinstance(obj, dict):
        if "address_book_v2" in obj or "address_book" in obj:
            return ("facebook", "uploads")
        if "contacts_contact_info" in obj:
            return ("instagram", "uploads")
        if "friends_v2" in obj or "friends" in obj:
            return ("facebook", "friends")
        if "relationships_following" in obj:
            return ("instagram", "following")
        if "relationships_followers" in obj:
            return ("instagram", "followers")
        return None
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        if "address_book" in name.lower():
            return ("facebook", "uploads")
        if "string_list_data" in obj[0]:
            return ("instagram", "following" if "following" in name.lower() else "followers")
        if "label_values" in obj[0] or "name" in obj[0]:
            return ("facebook", "friends")
    return None


def _rows(obj) -> list:
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in ("friends_v2", "friends", "relationships_following", "relationships_followers"):
            if isinstance(obj.get(k), list):
                return obj[k]
    return []


def _fb_name(row: dict) -> str:
    if isinstance(row.get("name"), str):
        return row["name"]
    for lv in row.get("label_values") or []:
        if isinstance(lv, dict) and str(lv.get("label") or "").lower() in ("name", "friend"):
            return str(lv.get("value") or "")
    return str(row.get("title") or "")


def _username(row: dict) -> tuple[str, str]:
    """`(username, profile url)` of an Instagram row."""
    href = ""
    for d in row.get("string_list_data") or []:
        if not isinstance(d, dict):
            continue
        href = href or str(d.get("href") or "")
        if d.get("value"):
            return str(d["value"]).strip(), href
    if row.get("title"):
        return str(row["title"]).strip(), href
    parts = [x for x in urlparse(href).path.split("/") if x and x != "_u"]
    return (parts[-1] if parts else ""), href


def _stamp(row: dict) -> int:
    for d in [row] + [x for x in row.get("string_list_data") or [] if isinstance(x, dict)]:
        if isinstance(d.get("timestamp"), (int, float)):
            return int(d["timestamp"])
    return 0


def _card(name: str, labels: list[str], handles: list | None = None) -> dict:
    out = {"name": name, "names": [{"display": name, "given": "", "family": ""}],
           "phones": [], "emails": [], "addresses": [], "organizations": [], "notes": "",
           "labels": labels}
    if handles:
        out["handles"] = handles
    return out


def _people(kind: tuple[str, str], obj, out: dict) -> int:
    """The list's people into `out` (`{export: {sid: record}}`); how many."""
    export, lst = kind
    label = LABELS[kind]
    book = out.setdefault(export, {})
    n = 0
    seen: dict[str, int] = {}
    for row in _rows(obj):
        if not isinstance(row, dict):
            continue
        if export == "facebook":
            name = fix_text(_fb_name(row)).strip()
            if not name:
                continue
            # No id in the export: the name and the day they became friends
            # (the same in every later export), a count for two the same.
            base = f"{name.casefold()}|{_stamp(row)}"
            seen[base] = seen.get(base, 0) + 1
            sid = "facebook:" + hashlib.sha256(
                f"{base}|{seen[base]}".encode()).hexdigest()[:20]
            rec = book.get(sid) or {"card": _card(name, []), "meta": {"export": "facebook",
                                                                       "lists": []}}
        else:
            user, href = _username(row)
            user = fix_text(user)
            if not user:
                continue
            sid = "instagram:" + user.lower()
            rec = book.get(sid) or {
                "card": _card(user, [], [{"network": "instagram", "network_name": "Instagram",
                                          "id": sid, "username": user,
                                          "url": href or f"https://www.instagram.com/{user}"}]),
                "meta": {"export": "instagram", "lists": []}}
        if label not in rec["card"]["labels"]:
            rec["card"]["labels"] = sorted(rec["card"]["labels"] + [label])
        if lst not in rec["meta"]["lists"]:
            rec["meta"]["lists"] = sorted(rec["meta"]["lists"] + [lst])
        book[sid] = rec
        n += 1
    return n


# --- uploaded contacts ------------------------------------------------------------

_PHONEISH = re.compile(r"^\+?[\d\s().-]{6,}$")


def _upload_rows(obj) -> list:
    if isinstance(obj, list):
        return obj
    if not isinstance(obj, dict):
        return []
    for k in ("address_book_v2", "address_book", "contacts_contact_info"):
        v = obj.get(k)
        if isinstance(v, dict):
            v = v.get("address_book")
        if isinstance(v, list):
            return v
    return []


def _strings(x, depth: int = 0):
    """Every string value under `x` (a few levels: label_values' `vec` and
    `dict` rows nest)."""
    if depth > 5:
        return
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v, depth + 1)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v, depth + 1)


def _upload_entry(row: dict) -> tuple[str, list[str]]:
    """`(name, contact points)` of one uploaded address-book entry."""
    name, points = "", []
    if isinstance(row.get("string_map_data"), dict):           # Instagram
        m = {k.casefold(): (v or {}).get("value") if isinstance(v, dict) else None
             for k, v in row["string_map_data"].items()}
        first = str(m.get("first name") or "").strip()
        last = str(m.get("surname") or m.get("last name") or "").strip()
        name = " ".join(x for x in (first, last) if x)
        points = [str(v) for k, v in m.items() if v and "contact" in k]
    elif isinstance(row.get("label_values"), list):             # Facebook's newest
        for lv in row["label_values"]:
            if not isinstance(lv, dict):
                continue
            label = str(lv.get("label") or "").casefold()
            if not name and "name" in label and isinstance(lv.get("value"), str):
                name = lv["value"]
            elif "name" not in label:
                points += list(_strings({k: v for k, v in lv.items() if k != "label"}))
    else:                                                       # Facebook
        name = str(row.get("name") or "")
        for d in row.get("details") or []:
            if isinstance(d, dict) and d.get("contact_point"):
                points.append(str(d["contact_point"]))
    return fix_text(name).strip(), [fix_text(p).strip() for p in points if p]


def _uploads(kind: tuple[str, str], obj, out: dict) -> int:
    """An uploaded address book's entries with a name and a number or an
    address into `out[<network>_uploads]`; how many."""
    from .contacts import _EMAIL, e164, region

    reg = region()
    book = out.setdefault(f"{kind[0]}_uploads", {})
    n = 0
    for row in _upload_rows(obj):
        if not isinstance(row, dict):
            continue
        name, points = _upload_entry(row)
        phones, emails = set(), set()
        for p in points:
            if _EMAIL.fullmatch(p):
                emails.add(p.lower())
            elif _PHONEISH.match(p) and (num := e164(p, reg)):
                phones.add(num)
        if not name or not (phones or emails):
            continue
        key = f"{name.casefold()}|{'|'.join(sorted(phones | emails))}"
        sid = f"{kind[0]}-upload:" + hashlib.sha256(key.encode()).hexdigest()[:20]
        card = _card(name, [])
        card["phones"] = [{"value": p} for p in sorted(phones)]
        card["emails"] = [{"value": e} for e in sorted(emails)]
        book[sid] = {"card": card, "meta": {"export": kind[0], "upload": True}}
        n += 1
    return n


def _take(out: dict, counts: dict, kind, raw: bytes, fname: str) -> None:
    try:
        obj = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return
    kind = kind or _guess(obj, fname)
    if kind:
        key = f"{kind[0]}_{kind[1]}"
        n = _uploads(kind, obj, out) if kind[1] == "uploads" else _people(kind, obj, out)
        counts[key] = counts.get(key, 0) + n


# --- the owner's own profile ------------------------------------------------------
#
# Facebook's personal_information/profile_information/profile_information.json
# (`profile_v2`) and Instagram's personal_information.json (`profile_user`)
# say the exporter's own name, email, phone and username: what a bridge
# leaves out of a sign-in (Messenger gives no email), filled in from
# `owner-networks.json` (bridges.owner_networks, contract §6.26 `profile`).

PROFILE_FILES = ("profile_information.json", "personal_information.json")
PROFILE_FIELDS = ("name", "email", "phone", "username")
_IG_KEYS = {"email": "email", "phone number": "phone", "phone": "phone",
            "username": "username", "name": "name"}


def _profile_file(path: str) -> bool:
    return path.replace("\\", "/").rsplit("/", 1)[-1].lower() in PROFILE_FILES


def _text(v) -> str:
    return fix_text(v).strip() if isinstance(v, str) else ""


def profile_of(obj) -> tuple[str, dict] | None:
    """`(network, {name?, email?, phone?, username?})` from a profile file's
    JSON, only the fields it fills; None when it is not one."""
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("profile_v2"), dict):                     # Facebook
        p = obj["profile_v2"]
        emails = (p.get("emails") or {}).get("emails") if isinstance(p.get("emails"), dict) else None
        phones = p.get("phone_numbers") if isinstance(p.get("phone_numbers"), list) else []
        phone = next((_text(x.get("phone_number")) for x in phones
                      if isinstance(x, dict) and _text(x.get("phone_number"))), "")
        got = {"name": _text((p.get("name") or {}).get("full_name")
                             if isinstance(p.get("name"), dict) else ""),
               "email": _text(emails[0]) if isinstance(emails, list) and emails else "",
               "phone": phone, "username": _text(p.get("username"))}
        return "facebook", {k: v for k, v in got.items() if v}
    rows = obj.get("profile_user")                                  # Instagram
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        smd = rows[0].get("string_map_data")
        got: dict[str, str] = {}
        if isinstance(smd, dict):
            for key, val in smd.items():
                field = _IG_KEYS.get(str(key).strip().lower())
                v = _text(val.get("value")) if isinstance(val, dict) else ""
                if field and v and field not in got:
                    got[field] = v
        return "instagram", got
    return None


def _profile_raw(raw: bytes) -> tuple[str, dict] | None:
    try:
        return profile_of(json.loads(raw.decode("utf-8-sig")))
    except (UnicodeDecodeError, ValueError):
        return None


def profiles_zip(zf: zipfile.ZipFile) -> dict[str, dict]:
    """`{network: fields}` from an export zip's profile files (small; read
    alone, never the rest)."""
    out: dict[str, dict] = {}
    for info in zf.infolist()[:MAX_MEMBERS]:
        if _profile_file(info.filename) and info.file_size <= MAX_FILE:
            got = _profile_raw(zf.read(info))
            if got:
                out[got[0]] = got[1]
    return out


def profiles_bytes(data: bytes, name: str = "") -> dict[str, dict]:
    """The same from one JSON file."""
    if data[:4] == b"PK\x03\x04" or data.lstrip()[:1] not in (b"{",):
        return {}
    got = _profile_raw(data)
    return {got[0]: got[1]} if got else {}


def save_owner_profiles(profiles: dict[str, dict]) -> dict[str, list[str]]:
    """Each network's profile as its export says it into owner-networks.json
    (0600, written whole and atomically), replacing that network's entry and
    keeping the others'. The owner's book only: another account's import
    writes nothing. `{network: [the fields kept]}`, never the values."""
    from . import bridges, contacts

    if not profiles or not contacts.is_owner():
        return {}
    path = bridges.owner_networks_path()
    have = bridges.owner_networks()
    said: dict[str, list[str]] = {}
    for net, fields in profiles.items():
        kept = {k: fields[k] for k in PROFILE_FIELDS if fields.get(k)}
        have[net] = kept
        said[net] = list(kept)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(have, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return said


def parse_zip(zf: zipfile.ZipFile) -> tuple[dict, dict, bool]:
    """`(records by export, counts by list, an HTML export seen)` from an
    open zip (a path's, so a whole export is never read into memory: only
    its list files are). No list is `({}, {}, html)`, not an error: the zip
    may hold a message history instead (contacts_history.py)."""
    out: dict[str, dict] = {}
    counts: dict[str, int] = {}
    html = False
    for info in zf.infolist()[:MAX_MEMBERS]:
        kind = _kind_of(info.filename)
        if kind is None:
            html = html or info.filename.lower().endswith(
                ("your_friends.html", "followers_1.html", "following.html",
                 "your_address_books.html", "synced_contacts.html"))
            continue
        if info.file_size > MAX_FILE:
            continue
        _take(out, counts, kind, zf.read(info), info.filename)
    return out, counts, html


def parse(data: bytes, name: str = "") -> tuple[dict, dict]:
    """`(records by export, counts by list)` from a .zip or a JSON file.
    NotAnExport when it holds no list this knows."""
    out: dict[str, dict] = {}
    counts: dict[str, int] = {}

    def take(kind, raw: bytes, fname: str) -> None:
        _take(out, counts, kind, raw, fname)

    if data[:4] == b"PK\x03\x04":
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise NotAnExport("not a zip that opens") from e
        out, counts, html = parse_zip(zf)
        if not counts:
            raise NotAnExport(
                "that export is HTML: download it again as JSON" if html else
                "no Facebook friends, Instagram followers/following or uploaded contacts "
                "in that zip")
    else:
        if data.lstrip()[:1] == b"<":
            raise NotAnExport("that is HTML: download the export as JSON")
        take(_kind_of(name) if name else None, data, name)
        if not counts:
            raise NotAnExport("not a Facebook friends, Instagram followers/following or "
                              "uploaded contacts list")
    return out, counts
