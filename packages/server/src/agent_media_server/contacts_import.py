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
"""

from __future__ import annotations

import hashlib
import io
import json
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
        if "friends_v2" in obj or "friends" in obj:
            return ("facebook", "friends")
        if "relationships_following" in obj:
            return ("instagram", "following")
        if "relationships_followers" in obj:
            return ("instagram", "followers")
        return None
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
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


def parse(data: bytes, name: str = "") -> tuple[dict, dict]:
    """`(records by export, counts by list)` from a .zip or a JSON file.
    NotAnExport when it holds no list this knows."""
    out: dict[str, dict] = {}
    counts: dict[str, int] = {}

    def take(kind, raw: bytes, fname: str) -> None:
        try:
            obj = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            return
        kind = kind or _guess(obj, fname)
        if kind:
            key = f"{kind[0]}_{kind[1]}"
            counts[key] = counts.get(key, 0) + _people(kind, obj, out)

    if data[:4] == b"PK\x03\x04":
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise NotAnExport("not a zip that opens") from e
        html = False
        for info in zf.infolist()[:MAX_MEMBERS]:
            kind = _kind_of(info.filename)
            if kind is None:
                html = html or info.filename.lower().endswith(
                    ("your_friends.html", "followers_1.html", "following.html"))
                continue
            if info.file_size > MAX_FILE:
                continue
            take(kind, zf.read(info), info.filename)
        if not counts:
            raise NotAnExport(
                "that export is HTML: download it again as JSON" if html else
                "no Facebook friends or Instagram followers/following list in that zip")
    else:
        if data.lstrip()[:1] == b"<":
            raise NotAnExport("that is HTML: download the export as JSON")
        take(_kind_of(name) if name else None, data, name)
        if not counts:
            raise NotAnExport("not a Facebook friends or Instagram followers/following list")
    return out, counts
