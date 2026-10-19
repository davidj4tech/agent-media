"""The timeline's past: the phone's SMS and MMS, and every bridged room's
history on the homeserver, counted into the contacts timeline once.

docs/proposals/2026-10-17-contacts.md, "Message history" (David, 19 Oct
2026: SMS history "… same goes with the other Bridges too actually").
Counts only, as everywhere in the timeline (timeline.py): per contact,
channel and day, in and out. No body, subject, message id or number is
kept; a number is read only to find whose a message is.

**The phone** (the owner's book, over `MEDIA_CONTACTS_ADB` as the contacts
and the call log are read), in short scripts rather than one long one (one
script for 3,520 SMS and 1,704 MMS did not finish in 900 s, 19 Oct 2026):
`content://sms` (`date`, `type`, `address` — never `body`; type 1 is in, 2
out, the rest left out); then `content://mms` (`_id`, `date` in seconds,
`msg_box` 1 in, 2 and 4 out); then each MMS's addresses from
`content://mms/<id>/addr` (type 137 from, 151 to, 130 cc, 129 bcc),
`MMS_BATCH` MMS a script, oldest first, a short random pause between. The
MMS pass is resumable: after each batch its last id and the counts read so
far (per contact and day, no number) are kept in the `backfill` table's
`phone:mms` row, so a run cut short goes on from there, and a later run reads
only newer MMS. Only a message with one other party counts: a group MMS is
no one's, as a group room is. Channel `sms`.

**The bridged rooms**: each room this server holds that is a contact's
(contacts.for_room: one-to-one rooms only) on a network the timeline knows
(timeline._NETWORKS), paged back through `/rooms/<id>/messages?dir=b` with
the room's own client (matrix.Room's `_get`, the canvas's token), gently
(`MEDIA_TIMELINE_BACKFILL_PAUSE_S`, 0.25 s between pages). The messages are
already in Matrix; this only counts them. Resumable: each room's next token
and the counts of the oldest day not yet read whole are kept in the
ledger's `backfill` table, so a run cut short (a restart, the homeserver
down) goes on where it stopped and a day split across two runs is counted
once, whole. A room read to its start is done and not read again.

**Not the bridges' own backfill.** mautrix bridges can be asked to fetch a
chat's older history from the network into Matrix. That is not done here:
it would write those messages into the rooms (a second copy of them, in
Matrix, of what the network already keeps), which is the doubling up David
did not want; the timeline needs only counts, and counts what Matrix and the
phone already hold.

Both write through timeline.import_days: merged with what is there by the
larger count, so the live rollup (which counts the newest messages of the
same rooms) and this never add up to more than happened, and running it
again changes nothing. Where the phone's SMS and the Google Messages room
both count a day as `sms`, the larger count stands.

The job runs in the server's process (the rooms are held there, not in a
CLI), started by `POST /contacts/timeline/backfill` (the owner, or this
host's own token) and watched by `GET` of the same; `… contacts backfill`
asks the running server.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse

log = logging.getLogger("agent-media.server.timeline_backfill")

#: Messages asked for per page, going back.
PAGE = 100
#: Pages between writes of what is counted so far (and the token).
FLUSH_PAGES = 10
PAUSE_S = 0.25
#: One script on the phone, at most: the SMS, the MMS list, or one batch of
#: MMS addresses (an MMS's addresses are one query each, so they go in
#: batches; 1,704 of them in one script did not finish in 900 s).
PHONE_TIMEOUT_S = 600.0
#: MMS whose addresses are read per script.
MMS_BATCH = 100
#: Between two batches, a short pause (seconds, a little random).
MMS_PAUSE_S = (1.0, 3.0)
#: The backfill table's row for the MMS pass (never a room id: those start
#: with "!").
MMS_ROW = "phone:mms"

#: content://sms `type`: 1 inbox, 2 sent (3 draft, 4 outbox, 5 failed, 6
#: queued are left out — not yet, or never, a message between two people).
_SMS_DIR = {"1": "in", "2": "out"}
#: content://mms `msg_box`: 1 inbox, 2 sent, 4 outbox (sent, still going).
_MMS_DIR = {"1": "in", "2": "out", "4": "out"}
#: content://mms/<id>/addr `type`: 137 from, 151 to, 130 cc, 129 bcc.
_FROM, _TO = "137", ("151", "130", "129")
#: What Android writes for the phone's own address on a sent MMS.
_SELF_TOKEN = "insert-address-token"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS backfill (
  room TEXT PRIMARY KEY, token TEXT, done INTEGER NOT NULL DEFAULT 0,
  carry TEXT, counted INTEGER NOT NULL DEFAULT 0, at REAL);
"""

_JOB_LOCK = threading.Lock()
_JOB: dict = {"running": False}


def _nonce() -> str:
    return secrets.token_hex(8)


def _pause() -> float:
    try:
        return max(0.0, float(os.environ.get("MEDIA_TIMELINE_BACKFILL_PAUSE_S") or PAUSE_S))
    except ValueError:
        return PAUSE_S


def _add(sums: dict, cid: str, channel: str, day: str, direction: str, n: int = 1) -> None:
    s = sums.setdefault((cid, channel, day), [0, 0])
    s[0 if direction == "in" else 1] += n


# --- the phone ------------------------------------------------------------------

def _marker(nonce: str) -> str:
    return f"@@AM-{nonce}@@"


def sms_script(nonce: str, since_ms: int = 0) -> str:
    """The SMS: date, type and address, never the body."""
    m = _marker(nonce)
    return "; ".join([
        f"echo '{m} sms'",
        "content query --uri content://sms --projection date:type:address "
        f"--where 'date>={since_ms}' --sort date",
        f"echo '{m} end'"])


def mms_list_script(nonce: str, since_ms: int = 0) -> str:
    """The MMS: id, date (seconds) and box, never a part."""
    m = _marker(nonce)
    return "; ".join([
        f"echo '{m} mms'",
        "content query --uri content://mms --projection _id:date:msg_box "
        f"--where 'date>={since_ms // 1000}'",
        f"echo '{m} end'"])


def mms_addr_script(nonce: str, ids: list[str]) -> str:
    """One batch of MMS addresses, each after its own marker."""
    m = _marker(nonce)
    ids = [i for i in ids if i.isdigit()]
    if not ids:
        return f"echo '{m} end'"
    return (f"for i in {' '.join(ids)}; do echo \"{m} addr-$i\"; "
            "content query --uri content://mms/$i/addr --projection address:type; done; "
            f"echo '{m} end'")


def phone_script(nonce: str, since_ms: int = 0) -> str:
    """Every script the phone is sent, joined (what each asks, for a look)."""
    return "; ".join([sms_script(nonce, since_ms), mms_list_script(nonce, since_ms),
                      mms_addr_script(nonce, ["1"])])


def _cols(row: str, keys: tuple) -> dict:
    """A `content query` row `k1=…, k2=…, last=…`: the last key takes the
    rest, since only an address could hold a comma."""
    out, rest = {}, row
    for k in keys[:-1]:
        m = re.match(rf"{k}=([^,]*), ", rest)
        if not m:
            return {}
        out[k] = m.group(1)
        rest = rest[m.end():]
    last = keys[-1]
    if not rest.startswith(f"{last}="):
        return {}
    out[last] = rest[len(last) + 1:].strip()
    return out


def _ask_phone(script: str, nonce: str) -> dict[str, str]:
    """Run one script on the phone; its sections. Unreachable when the
    phone does not answer, or answers with an error."""
    from . import contacts_android

    try:
        got = subprocess.run(contacts_android.command() + [script],
                             capture_output=True, text=True, timeout=PHONE_TIMEOUT_S,
                             encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise contacts_android.Unreachable(
            f"the phone did not answer in {int(PHONE_TIMEOUT_S)} s") from None
    except OSError as e:
        raise contacts_android.Unreachable(f"could not run the phone tool ({e})") from None
    if got.returncode != 0:
        last = (got.stderr or got.stdout or "").strip().splitlines()[-1:] or [""]
        raise contacts_android.Unreachable(f"the phone tool exited {got.returncode}: "
                                           f"{last[0][:200]}")
    return contacts_android.sections(got.stdout, nonce)


def _mms_pause() -> float:
    if (os.environ.get("MEDIA_TIMELINE_BACKFILL_PAUSE_S") or "").strip() == "0":
        return 0.0
    import random

    return random.uniform(*MMS_PAUSE_S)


def _sums_out(sums: dict) -> list:
    return [[c, ch, d, v[0], v[1]] for (c, ch, d), v in sums.items()]


def _sums_in(rows) -> dict:
    out: dict = {}
    for r in rows or []:
        try:
            out[(r[0], r[1], r[2])] = [int(r[3]), int(r[4])]
        except (IndexError, TypeError, ValueError):
            continue
    return out


def phone(since_ms: int = 0) -> dict:
    """The phone's SMS and MMS as `{(contact, "sms", day): [in, out]}`,
    with what was read and left out. Three steps, so no one script runs
    long: the SMS; the MMS list; then the MMS addresses `MMS_BATCH` at a
    time, oldest id first, each batch's counts and its last id kept in the
    backfill table (`phone:mms`), so a run cut short goes on from there and
    an MMS is counted once. The MMS counts kept are every MMS read so far
    (per contact and day, no number), so a day's SMS and MMS are summed
    whole each run. Unreachable when the phone is, before anything is read;
    a batch that fails after others were read ends the pass with what was
    read (`mms_error`), the rest next run."""
    from . import contacts, contacts_android

    nonce = _nonce()
    sms_parts = _ask_phone(sms_script(nonce, since_ms), nonce)
    region = contacts.region()
    phones = _by_phone()
    self_phones, _ = contacts._self_ids()
    sums: dict = {}
    stats = {"sms": 0, "mms": 0, "counted": 0, "nobody": 0, "groups": 0}

    def num(raw: str) -> str | None:
        return contacts.e164(raw, region) if raw and raw != _SELF_TOKEN else None

    for row in contacts_android.rows(sms_parts.get("sms") or ""):
        f = _cols(row, ("date", "type", "address"))
        direction = _SMS_DIR.get(f.get("type") or "")
        if not direction:
            continue
        stats["sms"] += 1
        who = phones.get(num(f.get("address") or "") or "")
        try:
            day = dt.date.fromtimestamp(int(f["date"]) / 1000).isoformat()
        except (KeyError, ValueError):
            continue
        if not who:
            stats["nobody"] += 1
            continue
        _add(sums, who, "sms", day, direction)
        stats["counted"] += 1

    # The MMS: what was read before (its counts and its last id), then the
    # rest in batches.
    st = _state(MMS_ROW) or {"token": None, "done": False, "carry": None, "counted": 0}
    carry = st["carry"] or {}
    mms_sums = _sums_in(carry.get("sums"))
    mstats = {k: int(carry.get(k) or 0) for k in ("mms", "counted", "nobody", "groups")}
    try:
        last = int(st["token"] or 0)
    except ValueError:
        last = 0
    mms_error = None
    try:
        nonce = _nonce()
        listed = _ask_phone(mms_list_script(nonce, since_ms), nonce)
        todo: list[tuple[int, str, str]] = []
        for row in contacts_android.rows(listed.get("mms") or ""):
            f = _cols(row, ("_id", "date", "msg_box"))
            direction = _MMS_DIR.get(f.get("msg_box") or "")
            try:
                mid = int(f.get("_id") or "")
                day = dt.date.fromtimestamp(int(f["date"])).isoformat()
            except (KeyError, ValueError):
                continue
            if direction and mid > last:
                todo.append((mid, day, direction))
        todo.sort()
        for i in range(0, len(todo), MMS_BATCH):
            batch = todo[i:i + MMS_BATCH]
            if i:
                time.sleep(_mms_pause())
            nonce = _nonce()
            parts = _ask_phone(mms_addr_script(nonce, [str(m) for m, _, _ in batch]), nonce)
            for mid, day, direction in batch:
                text = parts.get(f"addr-{mid}") or ""
                rows = [(a.get("type") or "", a.get("address") or "")
                        for a in (_cols(r, ("address", "type"))
                                  for r in contacts_android.rows(text)) if a]
                mstats["mms"] += 1
                frm = {n for t, a in rows if t == _FROM for n in [num(a)] if n} - self_phones
                to = {n for t, a in rows if t in _TO for n in [num(a)] if n} - self_phones
                if direction == "in":
                    # Without the phone's own number known, a received MMS's
                    # one "to" is taken to be the phone itself.
                    if not self_phones and len(to) == 1:
                        to = set()
                    others = frm | to
                else:
                    others = to
                if len(others) != 1:
                    mstats["groups" if len(others) > 1 else "nobody"] += 1
                    continue
                who = phones.get(next(iter(others)))
                if not who:
                    mstats["nobody"] += 1
                    continue
                _add(mms_sums, who, "sms", day, direction)
                mstats["counted"] += 1
            last = batch[-1][0]
            _save(MMS_ROW, str(last), i + MMS_BATCH >= len(todo),
                  {"sums": _sums_out(mms_sums), **mstats}, mstats["counted"])
    except contacts_android.Unreachable as e:
        if not mms_sums and not stats["sms"]:
            raise
        mms_error = str(e)
    for key, (n_in, n_out) in mms_sums.items():
        _add(sums, key[0], key[1], key[2], "in", n_in)
        _add(sums, key[0], key[1], key[2], "out", n_out)
    out = {"sums": sums, "sms": stats["sms"], "mms": mstats["mms"],
           "counted": stats["counted"] + mstats["counted"],
           "nobody": stats["nobody"] + mstats["nobody"], "groups": mstats["groups"]}
    if mms_error:
        out["mms_error"] = mms_error
    return out


def _by_phone() -> dict:
    from . import timeline

    return timeline._by_phone()


# --- the bridged rooms ----------------------------------------------------------

def _connect():
    from . import timeline

    conn = timeline._connect()
    conn.executescript(_SCHEMA)
    return conn


def _state(room: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT token, done, carry, counted FROM backfill WHERE room=?",
                           (room,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        carry = json.loads(row[2]) if row[2] else None
    except ValueError:
        carry = None
    return {"token": row[0], "done": bool(row[1]), "carry": carry, "counted": row[3]}


def _save(room: str, token: str | None, done: bool, carry: dict | None, counted: int) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO backfill (room, token, done, carry, counted, at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(room) DO UPDATE SET token=excluded.token, done=excluded.done, "
            "carry=excluded.carry, counted=excluded.counted, at=excluded.at",
            (room, token, int(done), json.dumps(carry) if carry else None, counted, time.time()))
    finally:
        conn.close()


def room(r, cid: str, channel: str, *, mine: set, self_phones: set,
         pages: int | None = None) -> dict:
    """Count one room back to its start (or `pages` pages, for a test),
    writing every whole day as it goes. `{messages, days, done, error}`."""
    from . import matrix, timeline

    st = _state(r.id) or {"token": None, "done": False, "carry": None, "counted": 0}
    if st["done"]:
        return {"messages": 0, "days": 0, "done": True, "error": None}
    token, counted = st["token"], st["counted"]
    # day -> [in, out]; the oldest day reached is not whole until a page
    # older than it is read, so it is kept back (and carried to a next run).
    acc: dict[str, list[int]] = {}
    if st["carry"]:
        acc[st["carry"]["day"]] = [int(st["carry"]["in"]), int(st["carry"]["out"])]
    with r.lock:
        names = dict(r.members)
    messages = written = 0
    error = None
    done = False
    n = 0

    def flush(whole: bool) -> int:
        if not acc:
            return 0
        oldest = min(acc)
        ready = {d: v for d, v in acc.items() if whole or d > oldest}
        if not ready:
            return 0
        got = timeline.import_days({(cid, channel, d): v for d, v in ready.items()}, push_=False)
        for d in ready:
            acc.pop(d)
        return got["changed"]

    while pages is None or n < pages:
        params = {"dir": "b", "limit": str(PAGE)}
        if token:
            params["from"] = token
        try:
            data = r._get(r.config, f"/rooms/{matrix._q(r.id)}/messages", params, timeout=30.0)
        except (urllib.error.URLError, OSError, ValueError) as e:
            error = str(e) or type(e).__name__
            break
        n += 1
        chunk = data.get("chunk") or []
        for ev in chunk:
            if ev.get("type") != "m.room.message":
                continue
            content = ev.get("content") or {}
            if not content or content.get("msgtype") == "m.notice":
                continue             # a redacted one, or the bridge talking
            if (content.get("m.relates_to") or {}).get("rel_type") == "m.replace":
                continue             # an edit is the same message again
            try:
                day = dt.date.fromtimestamp(int(ev.get("origin_server_ts")) / 1000).isoformat()
            except (TypeError, ValueError):
                continue
            sender = ev.get("sender") or ""
            out = (sender == r.owner or sender in mine
                   or timeline._self_puppet(sender, names.get(sender, ""), self_phones))
            s = acc.setdefault(day, [0, 0])
            s[1 if out else 0] += 1
            messages += 1
        end = data.get("end") if chunk else None
        if not end:
            done = True
            break
        token = end
        if n % FLUSH_PAGES == 0:
            written += flush(False)
            _save(r.id, token, False, _carry(acc), counted + messages)
        if _pause():
            time.sleep(_pause())
    written += flush(done)
    _save(r.id, token, done, None if done else _carry(acc), counted + messages)
    return {"messages": messages, "days": written, "done": done, "error": error}


def _carry(acc: dict) -> dict | None:
    if not acc:
        return None
    day = min(acc)
    return {"day": day, "in": acc[day][0], "out": acc[day][1]}


def rooms(pages: int | None = None) -> dict:
    """Every held room that is a contact's, on a known network."""
    from . import contacts, matrix, timeline

    self_phones, _ = contacts._self_ids()
    mine = contacts.own_ghosts()
    out = {"rooms": 0, "done": 0, "messages": 0, "days": 0, "errors": 0}
    for r in list(matrix._ROOMS.values()):
        channel = timeline._NETWORKS.get(((r.network or {}).get("id") or ""))
        if not channel:
            continue
        who = contacts.for_room(r)
        if not who:
            continue
        out["rooms"] += 1
        got = room(r, who["id"], channel, mine=mine, self_phones=self_phones, pages=pages)
        out["messages"] += got["messages"]
        out["days"] += got["days"]
        out["done"] += int(got["done"])
        if got["error"]:
            out["errors"] += 1
            log.warning("timeline backfill: a room was not read to its start: %s", got["error"])
    return out


# --- the job --------------------------------------------------------------------

def status() -> dict:
    with _JOB_LOCK:
        return json.loads(json.dumps(_JOB))


def run(*, phone_on: bool = True, rooms_on: bool = True) -> dict:
    """The whole backfill, in this thread: the phone, then the rooms. The
    owner's book; said, never raised."""
    from . import contacts, contacts_android, timeline

    got: dict = {"phone": None, "rooms": None}
    with contacts.as_account(""):
        if phone_on and not contacts_android.available():
            got["phone"] = {"error": "no phone tool on this host"}
        elif phone_on:
            try:
                p = phone()
                w = timeline.import_days(p.pop("sums"), push_=False)
                got["phone"] = {**p, "days": w["changed"]}
            except contacts_android.Unreachable as e:
                got["phone"] = {"error": str(e)}
            _update(got)
        if rooms_on:
            got["rooms"] = rooms()
            _update(got)
        try:
            timeline.push()
        except Exception as e:  # noqa: BLE001 — the CRM down is not the backfill failing
            got["push_error"] = str(e)
    return got


def _update(got: dict) -> None:
    with _JOB_LOCK:
        _JOB.update(json.loads(json.dumps(got)))


def start() -> dict:
    """Start the job unless it is running; the job's state either way."""
    with _JOB_LOCK:
        if _JOB.get("running"):
            return {**json.loads(json.dumps(_JOB)), "started": False}
        _JOB.clear()
        _JOB.update({"running": True, "started_at": time.time(), "phone": None, "rooms": None})

    def go():
        try:
            got = run()
        except Exception as e:  # noqa: BLE001
            log.exception("timeline backfill failed")
            got = {"error": str(e) or type(e).__name__}
        with _JOB_LOCK:
            _JOB.update(json.loads(json.dumps(got)))
            _JOB.update({"running": False, "finished_at": time.time()})

    threading.Thread(target=go, name="timeline-backfill", daemon=True).start()
    return {**status(), "started": True}


# --- the CLI: asks the running server -------------------------------------------

def _ask(method: str, timeout: float = 15.0) -> tuple[int, dict | None]:
    import urllib.request

    from agent_media_core import phone_run

    req = urllib.request.Request(phone_run._base() + "/contacts/timeline/backfill",
                                 data=b"{}" if method == "POST" else None, method=method,
                                 headers={"Content-Type": "application/json",
                                          "X-Auth-Token": phone_run._token()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (OSError, ValueError):
        return 0, None


def say(st: dict) -> list[str]:
    """The job's state in lines: counts, never names."""
    out = []
    p = st.get("phone")
    if p:
        if p.get("error"):
            out.append(f"phone: not read ({p['error']})")
        else:
            out.append(f"phone: {p.get('sms', 0)} SMS and {p.get('mms', 0)} MMS read, "
                       f"{p.get('counted', 0)} a contact's, {p.get('groups', 0)} group MMS left "
                       f"out; {p.get('days', 0)} days changed")
            if p.get("mms_error"):
                out.append(f"phone: the MMS stopped partway ({p['mms_error']}); "
                           "run again to go on")
    r = st.get("rooms")
    if r:
        out.append(f"rooms: {r.get('done', 0)} of {r.get('rooms', 0)} read to their start, "
                   f"{r.get('messages', 0)} messages counted; {r.get('days', 0)} days changed"
                   + (f"; {r['errors']} cut short (run again to go on)" if r.get("errors") else ""))
    if st.get("error"):
        out.append(f"failed: {st['error']}")
    return out


def cmd_backfill(a) -> int:
    status_, st = _ask("GET" if a.status else "POST")
    if st is None:
        print("no server answered here (is the canvas running?)", file=sys.stderr)
        return 1
    if status_ != 200:
        print(f"refused ({status_}): {st.get('error') or ''}", file=sys.stderr)
        return 1
    if not a.status:
        print("started" if st.get("started") else "already running")
    if a.wait and not a.status:
        while st.get("running"):
            time.sleep(5)
            status_, got = _ask("GET")
            if got is None or status_ != 200:
                print("the server stopped answering; the job goes on there "
                      "(… contacts backfill --status)", file=sys.stderr)
                return 1
            st = got
    for line in say(st):
        print(line)
    if st.get("running"):
        print("running…")
    return 0


def add_commands(sub) -> None:
    p = sub.add_parser("backfill", help="count the phone's SMS/MMS and every bridged room's "
                                        "history into the timeline (in the running server)")
    p.add_argument("--status", action="store_true", help="only say how the job is doing")
    p.add_argument("--no-wait", dest="wait", action="store_false",
                   help="start it and return")
    p.set_defaults(fn=cmd_backfill, wait=True)
