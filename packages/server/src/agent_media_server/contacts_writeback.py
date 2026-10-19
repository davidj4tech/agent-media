"""A Join writes back to Google (contacts.py's step 4, its first part).

David, 19 Oct 2026: "It'd be nice to add things like Facebook ID to Google
contacts that I've set as the same person as a Google contact" — chosen: on
Join, not asked each time. When `same()` joins a bridged or exported person
to a contact, that contact's links on the other networks are added to its
Google entry as websites with a label ("Facebook",
https://www.facebook.com/profile.php?id=…). The write goes through the
phone, as the read does (contacts_android.py): a `content insert` on the
person's `com.google` raw contact, as shell uid, which marks it dirty, and
Android syncs it to Google. No Cloud project, and the People API sign-in
stays read-only.

* **Only adds.** A website the entry already has (by address, scheme,
  `www.` and a trailing slash aside) is not added again, and nothing is
  ever changed or removed; undoing a Join leaves the links (David removes
  them in Google Contacts).
* **What becomes a link**: a Messenger ghost's Facebook id, an Instagram
  export's username, a WhatsApp ghost's number (`wa.me`) — the public
  profiles; and every bridged account, whatever its network, as a link into
  Sasonica (David, 19 Oct 2026: "even if there's no public address, it would
  still be good to have the handle or ID stored. even just to know that
  there is a linked account"; chose the Sasonica link): "<network> chat",
  the one-to-one room's thread (`/app/t/<thread>`), or for someone met only
  in groups "<network> (groups)", their card (`/app/people/<id>`). Those
  need `MEDIA_PUBLIC_URL`, the server's public address; without it only
  the profiles are written.
* **Queued.** A Join returns at once; the write runs in a thread, and one
  the phone could not take (off Wi-Fi) waits in `google-writeback.json`
  (the book's state dir) for the next sync loop. A contact with no Google
  entry on the phone (WhatsApp's alone, a device contact) is let go and
  said in the file's `done`.
* The owner's book only (the phone is the owner's); off with
  `MEDIA_CONTACTS_WRITEBACK=0`.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

from . import contacts, contacts_android

URI = contacts_android.URI
WEBSITE = "vnd.android.cursor.item/website"
GOOGLE = "com.google"
#: A website row's type: TYPE_CUSTOM, so `data3` is its label.
CUSTOM = 0
TIMEOUT_S = 60.0
#: Tries before a queued Join is let go (one a sync loop, so a day or so).
MAX_TRIES = 100

_LOCK = threading.Lock()
_FLUSHING = threading.Lock()

_FB_GHOST = re.compile(r"^@facebook_(\d{5,20}):")
_WA_GHOST = re.compile(r"^@whatsapp_(\d{6,15}):")
_IG_USER = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def enabled() -> bool:
    return (os.environ.get("MEDIA_CONTACTS_WRITEBACK") or "1").strip() != "0"


def path() -> Path:
    return contacts.account_state_dir() / "google-writeback.json"


def _load() -> dict:
    try:
        data = json.loads(path().read_text())
    except (OSError, ValueError):
        data = {}
    return {"pending": data.get("pending") or {}, "done": data.get("done") or {}}


def _save(data: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def links(card: dict) -> list[tuple[str, str]]:
    """`[(label, url)]`: the card's handles that have a public address."""
    out: dict[str, tuple[str, str]] = {}
    for h in card.get("handles") or []:
        hid, net = h.get("id") or "", (h.get("network") or "").lower()
        url = label = ""
        if m := _FB_GHOST.match(hid):
            label, url = "Facebook", f"https://www.facebook.com/profile.php?id={m.group(1)}"
        elif m := _WA_GHOST.match(hid):
            label, url = "WhatsApp", f"https://wa.me/{m.group(1)}"
        elif net == "instagram" and _IG_USER.match(h.get("username") or ""):
            label, url = "Instagram", f"https://www.instagram.com/{h['username']}/"
        if url:
            out.setdefault(_norm(url), (label, url))
    return list(out.values())


def chat_links(cid: str, card: dict, metas: dict[str, dict], base: str) -> list[tuple[str, str]]:
    """`[(label, url)]` into Sasonica for each bridged account on the card:
    `metas` is each ghost's record meta (`network_name`, `rooms`,
    `groups_only`)."""
    from urllib.parse import quote

    from . import matrix

    if not base:
        return []
    out: dict[str, tuple[str, str]] = {}
    for h in card.get("handles") or []:
        meta = metas.get(h.get("id") or "")
        if meta is None:
            continue                   # an export's person: no room
        net = meta.get("network_name") or h.get("network_name") or "Matrix"
        rooms = meta.get("rooms") or []
        if rooms and not meta.get("groups_only"):
            label, url = f"{net} chat", f"{base}/app/t/{quote(matrix.thread_of(rooms[0]), safe='')}"
        else:
            label, url = f"{net} (groups)", f"{base}/app/people/{quote(cid, safe='')}"
        out.setdefault(_norm(url), (label, url))
    return list(out.values())


def _base() -> str:
    return (os.environ.get("MEDIA_PUBLIC_URL") or "").strip().rstrip("/")


def _norm(url: str) -> str:
    u = re.sub(r"^[a-z]+://", "", (url or "").strip().lower())
    return re.sub(r"^www\.", "", u).rstrip("/")


def _raw_ids(card: dict) -> list[str]:
    """The phone's raw contacts behind the card, where one of them is
    Google's (the android source's `accounts`)."""
    out = []
    for s in card.get("sources") or []:
        if s.get("kind") == "android" and GOOGLE in (s.get("accounts") or []):
            out += [str(r) for r in s.get("raw") or [] if str(r).isdigit()]
    return out


class WriteError(Exception):
    """The phone answered but refused (`content` says so on its output and
    still exits 0): the Join is let go with the reason, not retried."""


def _shell(script: str) -> str:
    cmd = contacts_android.command()
    try:
        got = subprocess.run(cmd + [script], capture_output=True, text=True,
                             timeout=TIMEOUT_S, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise contacts_android.Unreachable(f"the phone did not answer in {int(TIMEOUT_S)} s") \
            from None
    except OSError as e:
        raise contacts_android.Unreachable(f"could not run {cmd[0]} ({e})") from None
    if got.returncode != 0:
        last = (got.stderr or got.stdout or "").strip().splitlines()[-1:] or [""]
        raise contacts_android.Unreachable(f"{cmd[0]} exited {got.returncode}: {last[0][:200]}")
    both = f"{got.stdout}\n{got.stderr}"
    if "Error while accessing provider" in both or re.search(r"^\S+Exception\b", both, re.M):
        why = next((ln for ln in both.splitlines() if "Exception" in ln), "refused")
        raise WriteError(why.strip()[:300])
    return got.stdout


def _google_raw(raw_ids: list[str]) -> str | None:
    """The first of `raw_ids` that is a live `com.google` raw contact now."""
    where = f"_id IN ({','.join(raw_ids)}) AND deleted=0 AND account_type='{GOOGLE}'"
    out = _shell(f"content query --uri {URI}/raw_contacts --projection _id "
                 f"--where \"{where}\" --sort _id")
    ids = re.findall(r"^Row: \d+ _id=(\d+)\s*$", out, re.M)
    return ids[0] if ids else None


def _websites(raw: str) -> set[str]:
    """The raw contact's websites, normalised. `data1` is the last column,
    so its value is the rest of the line."""
    where = f"raw_contact_id={raw} AND mimetype='{WEBSITE}'"
    out = _shell(f"content query --uri {URI}/data --projection _id:data1 "
                 f"--where \"{where}\"")
    return {_norm(m) for m in re.findall(r"^Row: \d+ _id=\d+, data1=(.*)$", out, re.M)}


def _insert(raw: str, label: str, url: str) -> None:
    binds = [f"raw_contact_id:i:{raw}", f"mimetype:s:{WEBSITE}", f"data1:s:{url}",
             f"data2:i:{CUSTOM}", f"data3:s:{label}"]
    _shell(f"content insert --uri {URI}/data "
           + " ".join(f"--bind {shlex.quote(b)}" for b in binds))


def write(cid: str) -> dict:
    """Add the contact's links to its Google entry: `{status, raw?, added?}`,
    status `added` | `nothing` (no links, or all there) | `no-google`.
    Unreachable raises (the Join stays queued), WriteError too (let go)."""
    conn = contacts._connect(create=False)
    if conn is None:
        return {"status": "no-google"}
    try:
        row = conn.execute("SELECT card FROM contacts WHERE id=?", (cid,)).fetchone()
        card = json.loads(row[0]) if row else {}
        ids = [h["id"] for h in card.get("handles") or [] if h.get("id")]
        metas = {sid: json.loads(meta) for sid, meta in conn.execute(
            f"SELECT sid, meta FROM records WHERE kind='bridges' "
            f"AND sid IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    finally:
        conn.close()
    if row is None:
        return {"status": "no-google"}
    want = links(card)
    seen = {_norm(u) for _, u in want}
    want += [x for x in chat_links(cid, card, metas, _base()) if _norm(x[1]) not in seen]
    if not want:
        return {"status": "nothing"}
    raw_ids = _raw_ids(card)
    raw = _google_raw(raw_ids) if raw_ids else None
    if raw is None:
        return {"status": "no-google"}
    have = _websites(raw)
    added = []
    for label, url in want:
        if _norm(url) not in have:
            _insert(raw, label, url)
            added.append(url)
    return {"status": "added" if added else "nothing", "raw": raw, "added": added}


def queue(cid: str) -> None:
    """After a Join: the contact's links go to Google, in a thread."""
    if not (enabled() and contacts.is_owner() and contacts_android.available()):
        return
    with _LOCK:
        data = _load()
        data["pending"].setdefault(cid, {"at": round(time.time(), 3), "tries": 0})
        _save(data)
    threading.Thread(target=flush, name="contacts-writeback", daemon=True).start()


def flush() -> dict:
    """Write every queued Join: `{written, waiting, error?}`. Run after a
    Join and by the sync loop; one at a time."""
    if not (enabled() and contacts.is_owner() and contacts_android.available()):
        return {"written": 0, "waiting": 0}
    with _FLUSHING:
        with _LOCK:
            pending = dict(_load()["pending"])
        written, error = 0, None
        for cid in pending:
            try:
                got = write(cid)
            except WriteError as e:
                got = {"status": "failed", "error": str(e)}
            except contacts_android.Unreachable as e:
                error = str(e)
                with _LOCK:
                    data = _load()
                    p = data["pending"].get(cid)
                    if p is not None:
                        p["tries"] = int(p.get("tries") or 0) + 1
                        p["last_error"] = error
                        if p["tries"] >= MAX_TRIES:
                            data["done"][cid] = {"status": "gave-up", "error": error,
                                                 "at": round(time.time(), 3)}
                            del data["pending"][cid]
                    _save(data)
                break                  # the phone is away: the rest wait too
            with _LOCK:
                data = _load()
                data["pending"].pop(cid, None)
                data["done"][cid] = {**got, "at": round(time.time(), 3)}
                _save(data)
            written += got["status"] == "added"
        with _LOCK:
            waiting = len(_load()["pending"])
        return {"written": written, "waiting": waiting, **({"error": error} if error else {})}
