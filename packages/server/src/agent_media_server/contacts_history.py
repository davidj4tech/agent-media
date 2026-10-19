"""Past conversations from the networks' own exports, as the timeline's days
(David, 19 Oct 2026: backfill the timeline from the exports).

The timeline (timeline.py) starts on the day this server first counted a
room. These read the years before from the downloads each network offers,
and add them as the same entries: per contact, per channel, per day, counts
in and out, never a word of what was said.

* **Messenger**, Facebook's "Download your information", JSON, Messages:
  `…/messages/{inbox,archived_threads,e2ee_cutover}/<thread>/message_N.json`
  (`{participants: [{name}], messages: [{sender_name, timestamp_ms}]}`;
  Facebook's escaped UTF-8 undone as for the friends list).
* **Instagram**, its data download, Messages:
  `your_instagram_activity/messages/inbox/<thread>/message_N.json`, the
  same shape.
* **Old chats**, Google Takeout, Mail, only the Chats label: an .mbox of the
  Google Talk (and gateway) chats Gmail kept. Only the headers are read
  (From, To, Cc, Date, X-Gmail-Labels), the file streamed line by line.

One-to-one conversations only: a group's messages belong to no one person
(the same rule a group room has). Who David is comes from the export
itself — the name in nearly every thread, the address in nearly every chat
— or `MEDIA_CONTACTS_SELF_NAME` / `MEDIA_CONTACTS_SELF`.

A thread's other person is matched by name to the one contact holding it,
one known on that network first (a bridged Messenger ghost, a friends-list
person); else they become an import person of their own ("Messenger
history"), as an unmatched friend does. A chat's address is matched by the
card's addresses and the old chat addresses (`aliases`), raw or decoded; an
unmatched address adds no one. Counts are summed per day before they are
written and merged by the larger, so importing the same export again
changes nothing. Nothing here logs a name or an address.
"""

from __future__ import annotations

import collections
import datetime as dt
import email.parser
import email.utils
import hashlib
import io
import json
import re
import zipfile

from .contacts_import import MAX_FILE, MAX_MEMBERS, fix_text

#: A message file of a thread, by its path in the zip.
_THREAD = re.compile(
    r"(?:^|/)messages/(inbox|archived_threads|e2ee_cutover)/([^/]+)/message_\d+\.json$", re.I)
#: The names an export gives an account that is gone: no one to count.
_NOBODY = {"facebook user", "instagram user", "deleted user", "instagram member",
           "participant", "unknown"}
#: The label an unmatched thread's person gets.
LABELS = {"messenger": "Messenger history", "instagram": "Instagram history"}
#: A contact's sources that say it is known on a network (contacts.py).
_ON = {"messenger": {"facebookgo", "facebook", "meta", "messenger"},
       "instagram": {"instagramgo", "instagram"}}
#: The share of threads (or chats) a name (or an address) must be in to be
#: taken as David himself.
SELF_SHARE = 0.6


class History:
    """What an export held: threads with their counts per day and sender,
    and the chats' headers summed by sender, recipients and day."""

    def __init__(self) -> None:
        # (network, thread folder) → {"people": set, "days": Counter((day, sender))}
        self.threads: dict[tuple[str, str], dict] = {}
        self.chats: collections.Counter = collections.Counter()   # (from, tos, day) → n
        self.chats_skipped = 0     # mail without the Chat label
        self.files = 0

    def __bool__(self) -> bool:
        return bool(self.threads or self.chats)


# --- reading ----------------------------------------------------------------------

def _day(ms: float) -> str | None:
    try:
        return dt.date.fromtimestamp(ms / 1000).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _network_of(path: str, hint: str = "") -> str:
    p = (path + " " + hint).lower()
    return "instagram" if "instagram" in p else "messenger"


def take_thread(hist: History, obj, network: str, thread: str) -> bool:
    """One message file into `hist`; False when it is not one."""
    if not isinstance(obj, dict) or not isinstance(obj.get("messages"), list):
        return False
    t = hist.threads.setdefault((network, thread), {"people": set(),
                                                    "days": collections.Counter()})
    for p in obj.get("participants") or []:
        name = fix_text(str((p or {}).get("name") or "")).strip() if isinstance(p, dict) else ""
        if name:
            t["people"].add(name)
    for m in obj["messages"]:
        if not isinstance(m, dict) or not isinstance(m.get("timestamp_ms"), (int, float)):
            continue
        day = _day(m["timestamp_ms"])
        sender = fix_text(str(m.get("sender_name") or "")).strip()
        if day and sender:
            t["days"][(day, sender)] += 1
    hist.files += 1
    return True


def _addr(a: str) -> str:
    """An address as matched: lower case, without an XMPP resource."""
    return a.strip().lower().split("/", 1)[0]


def _mail(raw: bytes, hist: History) -> None:
    msg = email.parser.BytesHeaderParser().parsebytes(raw)
    labels = msg.get("X-Gmail-Labels")
    if labels is not None and "chat" not in {x.strip().lower() for x in str(labels).split(",")}:
        hist.chats_skipped += 1
        return
    try:
        when = email.utils.parsedate_to_datetime(str(msg.get("Date") or ""))
    except (TypeError, ValueError, IndexError):
        return
    if when is None:
        return
    try:
        day = (when.astimezone() if when.tzinfo else when).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return
    frm = [_addr(a) for _, a in email.utils.getaddresses(
        [str(x) for x in msg.get_all("From") or []]) if "@" in a]
    tos = tuple(sorted({_addr(a) for _, a in email.utils.getaddresses(
        [str(x) for x in (msg.get_all("To") or []) + (msg.get_all("Cc") or [])]) if "@" in a}))
    if frm:
        hist.chats[(frm[0], tos, day)] += 1


def scan_mbox(f, hist: History) -> None:
    """An .mbox's headers, a line at a time (a file object of bytes)."""
    head: list[bytes] = []
    in_head = False
    started = False
    for line in f:
        if line.startswith(b"From "):
            if started and head:
                _mail(b"".join(head), hist)
            head, in_head, started = [], True, True
            continue
        if in_head:
            if line in (b"\n", b"\r\n"):
                in_head = False
            elif len(head) < 400:
                head.append(line)
    if started and head:
        _mail(b"".join(head), hist)
    if started:
        hist.files += 1


def scan_zip(zf: zipfile.ZipFile, name: str = "") -> History:
    """A Facebook, Instagram or Takeout zip's message files and mboxes;
    media and everything else left unread."""
    hist = History()
    for info in zf.infolist()[:MAX_MEMBERS]:
        fn = info.filename.replace("\\", "/")
        if fn.lower().endswith(".mbox"):
            with zf.open(info) as f:
                scan_mbox(f, hist)
            continue
        m = _THREAD.search(fn)
        if not m or info.file_size > MAX_FILE:
            continue
        try:
            obj = json.loads(zf.read(info).decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            continue
        take_thread(hist, obj, _network_of(fn, name), m.group(2))
    return hist


def scan_bytes(data: bytes, name: str = "") -> History:
    """A file as it was sent: a zip, an .mbox, or one message_N.json."""
    if data[:4] == b"PK\x03\x04":
        try:
            return scan_zip(zipfile.ZipFile(io.BytesIO(data)), name)
        except zipfile.BadZipFile:
            return History()
    hist = History()
    if data[:5] == b"From ":
        scan_mbox(io.BytesIO(data), hist)
        return hist
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return hist
    take_thread(hist, obj, _network_of(name), "upload:" + hashlib.sha256(
        json.dumps(sorted(str((p or {}).get("name")) for p in (obj.get("participants") or [])
                          if isinstance(p, dict))).encode()).hexdigest()[:16]
        if isinstance(obj, dict) else "")
    return hist


# --- who David is ---------------------------------------------------------------------

def self_names(hist: History) -> set[str]:
    """David's names in the threads: `MEDIA_CONTACTS_SELF_NAME` (comma
    separated), and, per network, the name in nearly every thread."""
    from . import contacts

    out = {contacts.fold(x) for x in (contacts.setting("MEDIA_CONTACTS_SELF_NAME", "self_name")
                                      or "").split(",") if x.strip()}
    for net in {n for n, _ in hist.threads}:
        threads = [t for (n, _), t in hist.threads.items() if n == net and t["people"]]
        if len(threads) < 2:
            continue
        seen = collections.Counter(contacts.fold(p) for t in threads for p in t["people"])
        top, n = seen.most_common(1)[0]
        if n >= SELF_SHARE * len(threads):
            out.add(top)
    out.discard("")
    return out


def self_addresses(hist: History) -> set[str]:
    """David's addresses in the chats: `MEDIA_CONTACTS_SELF`'s, and the one
    in nearly every chat."""
    from . import contacts

    out = set(contacts._self_ids()[1])
    if hist.chats:
        seen: collections.Counter = collections.Counter()
        total = 0
        for (frm, tos, _day), n in hist.chats.items():
            total += n
            for a in {frm, *tos}:
                seen[a] += n
        if seen:
            top, n = seen.most_common(1)[0]
            if n >= SELF_SHARE * total:
                out.add(top)
    return out


# --- into the book --------------------------------------------------------------------

def _book_index(conn) -> dict:
    """The current book's names, addresses and old chat addresses."""
    from . import contacts

    names: dict[str, set] = {}
    on: dict[str, set] = {"messenger": set(), "instagram": set()}
    for cid, name, raw in conn.execute("SELECT id, name, card FROM contacts"):
        c = json.loads(raw)
        for n in {contacts.fold(name)} | {contacts.fold(x.get("display") or "")
                                          for x in c.get("names") or []}:
            if n:
                names.setdefault(n, set()).add(cid)
        for s in c.get("sources") or []:
            for net, ids in _ON.items():
                if (s.get("kind") == "bridges" and s.get("network") in ids) or (
                        s.get("kind") == "import" and (s.get("export") or "").split("-")[0]
                        in ({"facebook", "messenger"} if net == "messenger" else {"instagram"})):
                    on[net].add(cid)
        for h in c.get("handles") or []:
            for net, ids in _ON.items():
                if h.get("network") in ids:
                    on[net].add(cid)
    emails: dict[str, set] = {}
    for cid, em in conn.execute("SELECT contact, email FROM emails"):
        emails.setdefault((em or "").lower(), set()).add(cid)
    for cid, addr, real in conn.execute("SELECT contact, address, real FROM aliases"):
        for a in (addr, real):
            if a:
                emails.setdefault(a.lower(), set()).add(cid)
    return {"names": names, "on": on, "emails": emails}


def _by_name(idx: dict, name: str, net: str) -> str | None:
    from . import contacts

    hits = idx["names"].get(contacts.fold(name), set())
    known = hits & idx["on"].get(net, set())
    if len(known) == 1:
        return next(iter(known))
    return next(iter(hits)) if len(hits) == 1 else None


def _by_address(idx: dict, addr: str) -> str | None:
    from . import contacts

    for a in (addr, contacts.gateway_real(addr) or ""):
        hits = idx["emails"].get(a.lower(), set()) if a else set()
        if len(hits) == 1:
            return next(iter(hits))
    return None


def _sid(net: str, name: str) -> str:
    from . import contacts

    return f"{net}-history:" + hashlib.sha256(contacts.fold(name).encode()).hexdigest()[:20]


def apply(hist: History) -> dict:
    """The history into the current book's timeline (and an import person
    for each thread's unmatched other): the counts, never a name."""
    from . import contacts, contacts_import, timeline

    me = self_names(hist)
    my_addrs = self_addresses(hist)
    out = {"messenger_conversations": 0, "instagram_conversations": 0, "old_chats": 0,
           "groups_left_out": 0, "unmatched": 0, "unknown_self": 0, "new_people": 0}
    # The one-to-one threads and the person each is with.
    pairs = []                           # (net, other name, Counter((day, sender)))
    for (net, _tid), t in hist.threads.items():
        people = {p for p in t["people"] if contacts.fold(p) not in _NOBODY}
        others = {p for p in people if contacts.fold(p) not in me}
        if len(people) > 2:
            out["groups_left_out"] += 1
        elif len(others) == len(people):
            out["unknown_self"] += 1     # David not found among them
        elif len(others) == 1:
            pairs.append((net, next(iter(others)), t["days"]))

    with contacts._SYNC_LOCK, contacts._WRITE:
        conn = contacts._connect()
        try:
            idx = _book_index(conn)
            where: dict[tuple[str, str], str | None] = {}
            new: dict[str, dict] = {}
            for net, other, _days in pairs:
                key = (net, contacts.fold(other))
                if key in where:
                    continue
                where[key] = _by_name(idx, other, net)
                if where[key] is None:
                    new[_sid(net, other)] = {
                        "card": contacts_import._card(other, [LABELS[net]]),
                        "meta": {"export": f"{net}-history", "lists": ["messages"]}}
            nets = {net for net, _o, _d in pairs}
            if nets:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for net in nets:
                        conn.execute("DELETE FROM records WHERE kind='import' AND sid LIKE ?",
                                     (f"{net}-history:%",))
                    conn.executemany(
                        "INSERT INTO records (kind, sid, card, meta) VALUES ('import', ?, ?, ?)",
                        [(sid, json.dumps(r["card"]), json.dumps(r["meta"]))
                         for sid, r in new.items()])
                    contacts._rebuild(conn)
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                out["new_people"] = len(new)
                # Where each new person landed (their own, or joined by a kept Join).
                held: dict[str, str] = {}
                for cid, raw in conn.execute("SELECT id, card FROM contacts"):
                    for s in json.loads(raw).get("sources") or []:
                        if s.get("kind") == "import" and s.get("id") in new:
                            held[s["id"]] = cid
                for (net, folded), cid in list(where.items()):
                    if cid is None:
                        where[(net, folded)] = held.get(
                            f"{net}-history:" + hashlib.sha256(folded.encode()).hexdigest()[:20])
                idx = _book_index(conn) if hist.chats else idx
        finally:
            conn.close()
    contacts._fresh()

    sums: dict[tuple[str, str, str], list[int]] = {}
    for net, other, days in pairs:
        cid = where.get((net, contacts.fold(other)))
        if not cid:
            out["unmatched"] += 1
            continue
        out[f"{net}_conversations"] += 1
        for (day, sender), n in days.items():
            s = sums.setdefault((cid, net, day), [0, 0])
            s[1 if contacts.fold(sender) in me else 0] += n
    chats_seen: set[str] = set()
    nobody: set[str] = set()
    for (frm, tos, day), n in hist.chats.items():
        if frm in my_addrs:
            for a in tos:
                if a in my_addrs:
                    continue
                cid = _by_address(idx, a)
                if cid:
                    sums.setdefault((cid, "chat", day), [0, 0])[1] += n
                    chats_seen.add(cid)
                else:
                    nobody.add(a)
        else:
            cid = _by_address(idx, frm)
            if cid:
                sums.setdefault((cid, "chat", day), [0, 0])[0] += n
                chats_seen.add(cid)
            else:
                nobody.add(frm)
    out["old_chats"] = len(chats_seen)
    out["unmatched"] += len(nobody)
    got = timeline.import_days(sums)
    out["days"] = got["changed"]
    out["chats_without_the_label"] = hist.chats_skipped
    return out
