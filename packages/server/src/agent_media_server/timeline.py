"""A contact's timeline: daily rollups kept in the Drupal CRM.

docs/proposals/2026-10-17-contacts.md, "Timeline" (David, 18 Oct 2026).
One entry per contact, per channel, per day: counts in and out, call
minutes and a one-line summary, never message bodies. A note is an entry of
its own. They are written to the CRM's `crm_activity` entity
(integrations/drupal/crm_activity), which keeps the history; this host
keeps a ledger of what it wrote (`<state_dir>/timeline.db`) so a CRM that
cannot be reached loses nothing, and pushes again later.

**Sources**, each an adapter with its own switch (`MEDIA_TIMELINE_SOURCES`,
comma separated; default `texts,notes`):

* `texts` (on): the Matrix rooms this server holds that are a contact's
  (contacts.for_room), by the bridge's network — Google Messages is `sms`,
  Signal `signal`. A message from David (the room's owner, or his own
  number's puppet, `MEDIA_CONTACTS_SELF`) is out, anyone else's in.
* `notes` (on): written when asked — `… contacts note <who> <text>`, or
  `POST /contacts/<id>/notes`.
* `calls` (off): the phone's call log over adb (`MEDIA_CONTACTS_ADB`, as
  the contacts are read), matched by number.
* `email` (off): mail headers over IMAP (Gmail with an app password, or any
  server), matched by address; `~/.config/agent-media/timeline-imap.json`
  (or `MEDIA_TIMELINE_IMAP`, a path or the JSON) holds `{host, user,
  password, folder?}`. Only From, To, Cc and Date are fetched.

The rollup runs after each contacts sync (contacts._run) over the last
`MEDIA_TIMELINE_DAYS` (2: yesterday and today). A count never goes down: a
room holds only its newest messages, so a recount of a day it no longer
holds whole is less than the first. `MEDIA_TIMELINE=0` leaves it off.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import imaplib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid as uuidlib
from pathlib import Path

CHANNELS = ("sms", "signal", "messenger", "instagram", "teams", "whatsapp", "call", "email",
            "note")
SOURCES = ("texts", "calls", "email", "notes")
DEFAULT_SOURCES = ("texts", "notes")
DAYS = 2
#: A bridge's network to the channel its messages are.
#: A bridged room's network (`m.bridge` protocol id, matrix._network) to its
#: channel. mautrix-meta carries Messenger as `facebookgo` and Instagram as
#: `instagramgo` (19 Oct 2026); the older bridges' ids are kept beside them.
_NETWORKS = {"gmessages": "sms", "signal": "signal",
             "facebookgo": "messenger", "facebook": "messenger", "meta": "messenger",
             "messenger": "messenger", "instagramgo": "instagram", "instagram": "instagram",
             "teams": "teams", "msteams": "teams", "whatsapp": "whatsapp"}
#: The call log's types: 1 incoming, 2 outgoing, 3 missed, 4 voicemail, 5
#: rejected, 6 blocked (left out), 7 answered elsewhere.
_CALL_DIR = {"1": "in", "2": "out", "3": "in", "4": "in", "5": "in", "7": "in"}
NOTE_MAX = 4000
RESOURCE = "/jsonapi/crm_activity/crm_activity"
TYPE = "crm_activity--crm_activity"

_WRITE = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  key TEXT PRIMARY KEY, contact TEXT NOT NULL, channel TEXT NOT NULL, day TEXT NOT NULL,
  count_in INTEGER NOT NULL DEFAULT 0, count_out INTEGER NOT NULL DEFAULT 0,
  duration INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '',
  at REAL, crm_id TEXT, pushed TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS entries_contact ON entries(contact, day);
"""


# --- where things are -----------------------------------------------------------

def db_path() -> Path:
    """The current book's ledger (contacts.account_state_dir)."""
    from .contacts import account_state_dir

    return account_state_dir() / "timeline.db"


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return conn


#: What only the owner's book can count: the Matrix rooms and the phone are
#: the owner's (contacts.for_room).
OWNER_ONLY = ("texts", "calls")


def enabled() -> list[str]:
    """The sources switched on: `MEDIA_TIMELINE_SOURCES` (another account's
    `contacts.json` `timeline`), else texts and notes (calls and email are
    opt-in). Texts and calls are the owner's book's alone."""
    from . import contacts

    raw = contacts.setting("MEDIA_TIMELINE_SOURCES", "timeline")
    if raw is None or not raw.strip():
        got = list(DEFAULT_SOURCES)
    else:
        got = [s for s in (x.strip() for x in raw.split(",")) if s in SOURCES]
    return got if contacts.is_owner() else [s for s in got if s not in OWNER_ONLY]


def days(n: int | None = None) -> list[str]:
    """The days a rollup covers, oldest first, in this host's time zone."""
    if n is None:
        try:
            n = int(os.environ.get("MEDIA_TIMELINE_DAYS") or DAYS)
        except ValueError:
            n = DAYS
    today = dt.date.today()
    return [(today - dt.timedelta(days=i)).isoformat() for i in range(max(1, n) - 1, -1, -1)]


def _day_of(ts: float) -> str:
    return dt.date.fromtimestamp(ts).isoformat()


def key_of(channel: str, day: str, contact: str) -> str:
    """The CRM entry's source key: one per contact, channel and day,
    whichever host writes it."""
    return f"agent-media:{channel}:{day}:{contact}"


# --- the adapters ---------------------------------------------------------------
#
# Each answers events: `(contact id, channel, day, "in"|"out", seconds)`.

def _self_puppet(sender: str, name: str, self_phones: set) -> bool:
    """A puppet of David's own number: his texts sent from the phone."""
    from . import contacts

    if not self_phones:
        return False
    return bool(set(contacts.identities(sender, name, contacts.region())["phones"]) & self_phones)


def texts(window: list[str]) -> list[tuple]:
    """The Matrix rooms that are a contact's: each message in the window."""
    from . import contacts, matrix

    self_phones, _ = contacts._self_ids()
    # David's own ghosts (Instagram's puppet of him): what they send is his.
    mine_ghosts = contacts.own_ghosts()
    want = set(window)
    out = []
    for r in list(matrix._ROOMS.values()):
        channel = _NETWORKS.get(((r.network or {}).get("id") or ""))
        if not channel:
            continue
        who = contacts.for_room(r)
        if not who:
            continue
        with r.lock:
            msgs = [(m.get("at") or 0, (m.get("sender") or {}).get("id") or "")
                    for m in r.messages]
            names = dict(r.members)
        for at, sender in msgs:
            day = _day_of(at) if at else ""
            if day not in want:
                continue
            mine = (sender == r.owner or sender in mine_ghosts
                    or _self_puppet(sender, names.get(sender, ""), self_phones))
            out.append((who["id"], channel, day, "out" if mine else "in", 0))
    return out


def _by_phone() -> dict:
    from . import contacts

    idx = contacts._index() or {}
    return {num: next(iter(cids)) for num, cids in (idx.get("phones") or {}).items()
            if len(cids) == 1}


def _by_email() -> dict:
    from . import contacts

    idx = contacts._index() or {}
    return {em: next(iter(cids)) for em, cids in (idx.get("emails") or {}).items()
            if len(cids) == 1}


def _call_script(since_ms: int) -> str:
    return ("content query --uri content://call_log/calls "
            "--projection date:duration:type:number "
            f"--where 'date>={since_ms}' --sort date")


def _fields(row: str) -> dict:
    """A `content query` row: `date=…, duration=…, type=…, number=…` —
    number last, since only it could hold a comma."""
    out = {}
    rest = row
    for k in ("date", "duration", "type"):
        m = re.match(rf"{k}=([^,]*), ", rest)
        if not m:
            return {}
        out[k] = m.group(1)
        rest = rest[m.end():]
    if not rest.startswith("number="):
        return {}
    out["number"] = rest[len("number="):].strip()
    return out


def calls(window: list[str]) -> list[tuple]:
    """The phone's call log over adb, in the window, by number."""
    from . import contacts, contacts_android

    since = dt.datetime.combine(dt.date.fromisoformat(window[0]), dt.time()).timestamp()
    try:
        got = subprocess.run(contacts_android.command() + [_call_script(int(since * 1000))],
                             capture_output=True, text=True, timeout=60,
                             encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise contacts_android.Unreachable(f"the call log was not read ({e})") from None
    if got.returncode != 0:
        raise contacts_android.Unreachable(
            f"the call log was not read ({(got.stderr or got.stdout).strip()[:200]})")
    phones = _by_phone()
    want = set(window)
    out = []
    for row in contacts_android.rows(got.stdout):
        f = _fields(row)
        direction = _CALL_DIR.get(f.get("type") or "")
        num = contacts.e164(f.get("number"), contacts.region())
        if not direction or not num or num not in phones:
            continue
        try:
            day = _day_of(int(f["date"]) / 1000)
            secs = int(f.get("duration") or 0)
        except ValueError:
            continue
        if day in want:
            out.append((phones[num], "call", day, direction, secs))
    return out


def imap_settings() -> dict | None:
    from .contacts import account_config_dir, owner_env

    raw = (owner_env("MEDIA_TIMELINE_IMAP") or "").strip()
    try:
        if raw.startswith("{"):
            got = json.loads(raw)
        else:
            p = Path(raw).expanduser() if raw else account_config_dir() / "timeline-imap.json"
            if not p.is_file():
                return None
            got = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(got, dict) or not all(got.get(k) for k in ("host", "user", "password")):
        return None
    return {"host": got["host"], "user": got["user"], "password": got["password"],
            "folder": got.get("folder") or "[Gmail]/All Mail",
            "port": int(got.get("port") or 993)}


def _imap(cfg: dict):
    """The one seam the tests replace."""
    return imaplib.IMAP4_SSL(cfg["host"], cfg["port"], timeout=30)


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _imap_date(d: dt.date) -> str:
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def email_(window: list[str]) -> list[tuple]:
    """Mail headers over IMAP, a day at a time, by address."""
    cfg = imap_settings()
    if cfg is None:
        raise RuntimeError("no IMAP connection (timeline-imap.json)")
    emails = _by_email()
    me = cfg["user"].lower()
    out = []
    box = _imap(cfg)
    try:
        box.login(cfg["user"], cfg["password"])
        box.select(f'"{cfg["folder"]}"', readonly=True)
        for day in window:
            d = dt.date.fromisoformat(day)
            typ, data = box.search(None, "SINCE", _imap_date(d),
                                   "BEFORE", _imap_date(d + dt.timedelta(days=1)))
            ids = (data[0] or b"").split() if typ == "OK" and data else []
            for i in range(0, len(ids), 200):
                typ, got = box.fetch(b",".join(ids[i:i + 200]),
                                     "(BODY.PEEK[HEADER.FIELDS (FROM TO CC)])")
                if typ != "OK":
                    continue
                for part in got:
                    if not isinstance(part, tuple):
                        continue
                    msg = email.message_from_bytes(part[1])
                    sender = [a.lower() for _, a in email.utils.getaddresses(msg.get_all("From") or [])]
                    to = [a.lower() for _, a in email.utils.getaddresses(
                        (msg.get_all("To") or []) + (msg.get_all("Cc") or []))]
                    if me in sender:
                        for a in set(to):
                            if a in emails:
                                out.append((emails[a], "email", day, "out", 0))
                    else:
                        for a in set(sender):
                            if a in emails:
                                out.append((emails[a], "email", day, "in", 0))
    finally:
        try:
            box.logout()
        except Exception:  # noqa: BLE001 — closing is best effort
            pass
    return out


ADAPTERS = {"texts": texts, "calls": calls, "email": email_}


# --- rolling up -----------------------------------------------------------------

def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def summary_of(channel: str, n_in: int, n_out: int, secs: int) -> str:
    n = n_in + n_out
    if channel == "call":
        mins = round(secs / 60)
        return f"{_plural(n, 'call')}" + (f", {mins} min" if mins else "") + \
            f" ({n_in} in, {n_out} out)"
    word = {"sms": "text", "signal": "Signal message", "email": "email"}.get(channel, "message")
    return f"{_plural(n, word)} ({n_in} in, {n_out} out)"


def rollup(window: list[str] | None = None, push_: bool = True) -> dict:
    """Every enabled adapter over the window, summed per contact, channel and
    day, into the ledger (a count never goes down), then the changed entries
    pushed to the CRM. One adapter failing is said; the rest still count."""
    window = window or days()
    sums: dict[tuple, list[int]] = {}
    errors = {}
    for name in enabled():
        fn = ADAPTERS.get(name)
        if fn is None:
            continue
        try:
            for cid, channel, day, direction, secs in fn(window):
                s = sums.setdefault((cid, channel, day), [0, 0, 0])
                s[0 if direction == "in" else 1] += 1
                s[2] += secs
        except Exception as e:  # noqa: BLE001 — one source down is not all of them
            errors[name] = str(e) or type(e).__name__
    changed = 0
    with _WRITE:
        conn = _connect()
        try:
            for (cid, channel, day), (n_in, n_out, secs) in sums.items():
                key = key_of(channel, day, cid)
                old = conn.execute("SELECT count_in, count_out, duration FROM entries WHERE key=?",
                                   (key,)).fetchone()
                if old:
                    n_in, n_out, secs = max(n_in, old[0]), max(n_out, old[1]), max(secs, old[2])
                    if (n_in, n_out, secs) == tuple(old):
                        continue
                conn.execute(
                    "INSERT INTO entries (key, contact, channel, day, count_in, count_out, "
                    "duration, summary, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET count_in=excluded.count_in, "
                    "count_out=excluded.count_out, duration=excluded.duration, "
                    "summary=excluded.summary, at=excluded.at, pushed=NULL",
                    (key, cid, channel, day, n_in, n_out, secs,
                     summary_of(channel, n_in, n_out, secs), time.time()))
                changed += 1
        finally:
            conn.close()
    pushed = push() if push_ else {"pushed": 0, "unlinked": 0, "errors": 0}
    return {"window": window, "sources": enabled(), "changed": changed, **pushed,
            "source_errors": errors}


def add_note(cid: str, text: str) -> dict:
    """A note on a contact, today: kept, and pushed to the CRM."""
    text = (text or "").strip()
    if not text:
        raise ValueError("an empty note")
    if "notes" not in enabled():
        raise PermissionError("notes are switched off (MEDIA_TIMELINE_SOURCES)")
    text = text[:NOTE_MAX]
    key = f"agent-media:note:{uuidlib.uuid4()}"
    day = dt.date.today().isoformat()
    with _WRITE:
        conn = _connect()
        try:
            conn.execute("INSERT INTO entries (key, contact, channel, day, summary, at) "
                         "VALUES (?, ?, 'note', ?, ?, ?)", (key, cid, day, text, time.time()))
        finally:
            conn.close()
    got = push(only=key)
    return {"key": key, "day": day, "summary": text, "synced": got["pushed"] == 1}


# --- the CRM --------------------------------------------------------------------

def _payload(row: dict, uuid: str, crm_id: str | None) -> dict:
    data = {"type": TYPE,
            "attributes": {"channel": row["channel"], "day": row["day"],
                           "count_in": row["count_in"], "count_out": row["count_out"],
                           "duration": row["duration"], "summary": row["summary"],
                           "source_key": row["key"]},
            "relationships": {"contact": {"data": {"type": "crm_contact--person", "id": uuid}}}}
    if crm_id:
        data["id"] = crm_id
    return {"data": data}


def _existing(key: str) -> str | None:
    from . import contacts_drupal
    import urllib.parse

    got = contacts_drupal.get(f"{RESOURCE}?filter[source_key]={urllib.parse.quote(key)}")
    data = got.get("data") or []
    return data[0]["id"] if data else None


def push(only: str | None = None) -> dict:
    """The entries not yet in the CRM, written there: found by source key
    and updated, else made. A contact the CRM does not hold yet waits."""
    from . import contacts, contacts_drupal

    if not contacts_drupal.configured():
        return {"pushed": 0, "unlinked": 0, "errors": 0}
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        q = "SELECT * FROM entries WHERE pushed IS NULL" + (" AND key=?" if only else "")
        rows = [dict(r) for r in conn.execute(q, (only,) if only else ())]
    finally:
        conn.close()
    pushed = unlinked = errors = 0
    for row in rows:
        uuid = contacts.drupal_uuid(row["contact"])
        if not uuid:
            unlinked += 1
            continue
        crm_id, err = row["crm_id"], None
        try:
            crm_id = crm_id or _existing(row["key"])
            if crm_id:
                contacts_drupal.patch(f"{RESOURCE}/{crm_id}", _payload(row, uuid, crm_id))
            else:
                crm_id = contacts_drupal.post(RESOURCE, _payload(row, uuid, None))["data"]["id"]
            pushed += 1
        except Exception as e:  # noqa: BLE001 — kept, and tried again next time
            err = str(e) or type(e).__name__
            errors += 1
        with _WRITE:
            conn = _connect()
            try:
                conn.execute("UPDATE entries SET crm_id=?, pushed=?, error=? WHERE key=?",
                             (crm_id, None if err else dt.datetime.now().isoformat(timespec="seconds"),
                              err, row["key"]))
            finally:
                conn.close()
    return {"pushed": pushed, "unlinked": unlinked, "errors": errors}


# --- reading it -----------------------------------------------------------------

def _entry(row: dict, origin: str) -> dict:
    return {"key": row.get("key") or "", "channel": row["channel"], "day": row["day"],
            "count_in": int(row.get("count_in") or 0), "count_out": int(row.get("count_out") or 0),
            "duration": int(row.get("duration") or 0), "summary": row.get("summary") or "",
            "origin": origin, "synced": origin == "crm" or bool(row.get("pushed"))}


def timeline(cid: str, limit: int | str | None = 100) -> dict:
    """`GET /contacts/<id>/timeline`: this host's ledger for the contact,
    with what else the CRM holds for them (entries written in its UI, or by
    another writer), newest day first."""
    from . import contacts, contacts_drupal

    try:
        n = max(1, min(int(limit or 100), 1000))
    except (TypeError, ValueError):
        n = 100
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        mine = [dict(r) for r in conn.execute(
            "SELECT * FROM entries WHERE contact=? ORDER BY day DESC, at DESC LIMIT ?", (cid, n))]
    finally:
        conn.close()
    entries = [_entry(r, "local") for r in mine]
    seen = {e["key"] for e in entries} | {r["crm_id"] for r in mine if r.get("crm_id")}
    crm_error = None
    uuid = contacts.drupal_uuid(cid)
    if uuid and contacts_drupal.configured():
        try:
            got = contacts_drupal.get(f"{RESOURCE}?filter[contact.id]={uuid}"
                                      f"&sort=-day,-changed&page[limit]={min(n, 50)}")
            for item in got.get("data") or []:
                a = item.get("attributes") or {}
                if item.get("id") in seen or (a.get("source_key") or "") in seen:
                    continue
                entries.append(_entry({**a, "key": a.get("source_key") or item.get("id")}, "crm"))
        except Exception as e:  # noqa: BLE001 — the ledger still answers
            crm_error = str(e) or type(e).__name__
    order = {c: i for i, c in enumerate(CHANNELS)}
    entries.sort(key=lambda e: (e["day"], -order.get(e["channel"], len(order))), reverse=True)
    return {"entries": entries[:n], "sources": enabled(),
            "crm": None if not uuid else {"id": uuid, "error": crm_error}}


# --- the command line (under `… contacts`) --------------------------------------

def _who(q: str) -> tuple[str | None, list[dict]]:
    """The one contact `q` names (an id, or a name or number as typed)."""
    from . import contacts

    if re.fullmatch(r"[0-9a-f-]{36}", q) and contacts.card(q):
        return q, []
    got = contacts.listing(q, 5)["contacts"]
    exact = [c for c in got if contacts.fold(c["name"]) == contacts.fold(q)]
    if len(exact) == 1:
        return exact[0]["id"], []
    if len(got) == 1:
        return got[0]["id"], []
    return None, got


def _pick(q: str) -> str | None:
    cid, maybe = _who(q)
    if cid:
        return cid
    if maybe:
        print(f"'{q}' could be: " + "; ".join(c["name"] for c in maybe), file=sys.stderr)
    else:
        print(f"no contact matches '{q}'", file=sys.stderr)
    return None


def cmd_note(a) -> int:
    cid = _pick(a.who)
    if not cid:
        return 1
    try:
        got = add_note(cid, " ".join(a.text))
    except (ValueError, PermissionError) as e:
        print(f"note: {e}", file=sys.stderr)
        return 1
    print(f"noted on {got['day']}" + ("" if got["synced"] else " (kept here; not in the CRM yet)"))
    return 0


def cmd_timeline(a) -> int:
    cid = _pick(a.who)
    if not cid:
        return 1
    got = timeline(cid, a.limit)
    for e in got["entries"]:
        mark = "" if e["synced"] else "  (not in the CRM yet)"
        print(f"{e['day']}  {e['channel']:6}  {e['summary']}{mark}")
    if not got["entries"]:
        print("nothing yet")
    if got["crm"] and got["crm"]["error"]:
        print(f"the CRM was not read: {got['crm']['error']}", file=sys.stderr)
    return 0


def cmd_rollup(a) -> int:
    got = rollup(days(a.days), push_=not a.no_push)
    print(f"rollup {got['window'][0]}..{got['window'][-1]} ({', '.join(got['sources'])}): "
          f"{got['changed']} changed, {got['pushed']} pushed, {got['unlinked']} waiting for "
          f"their CRM person, {got['errors']} failed")
    for s, e in got["source_errors"].items():
        print(f"{s}: {e}", file=sys.stderr)
    return 1 if got["errors"] or got["source_errors"] else 0


def add_commands(sub) -> None:
    p = sub.add_parser("note", help="note something about a contact, today")
    p.add_argument("who", help="a name, number or contact id")
    p.add_argument("text", nargs="+")
    p.set_defaults(fn=cmd_note)
    p = sub.add_parser("timeline", help="a contact's timeline, newest first")
    p.add_argument("who")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=cmd_timeline)
    p = sub.add_parser("rollup", help="count the last days into the timeline now")
    p.add_argument("--days", type=int, default=None, help=f"how many days back (default {DAYS})")
    p.add_argument("--no-push", action="store_true", help="keep it here; write nothing to the CRM")
    p.set_defaults(fn=cmd_rollup)
