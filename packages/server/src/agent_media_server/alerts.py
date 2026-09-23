"""The alert store: what the watchers say is wrong, in one place (§6.17).

docs/proposals/2026-09-24-alerts-and-digests.md. A dozen timers on this host
watch something — a disk, a host, a login, the memory store — and each used to
keep its own state, dedupe its own TODOs and deliver its own way. Here a
watcher only says **what is true now**, every run:

    POST /alerts {"id": "disk.red5.root", "level": "warn", "title": …}
    POST /alerts {"id": "disk.red5.root", "level": "ok"}      # while fine

and this module decides what that *changes*:

    raised     ok/info → warn/needs         notify
    escalated  a higher level, or a higher `step` at the same one
               (disk: 90 → 95 → 98 %)       notify
    eased      down, but still warn+         quiet
    cleared    warn/needs → ok/info          quiet line; the TODO closes
    digest     a `kind: digest` report (a one-off body: describe, agenda,
               landscape) — kept as the latest per id, notify only at warn+

`confirm: N` holds a raise until the same level has been reported N times in
a row (host-watch's "two misses before crying wolf"); a clear is never held.
`every_s` is how often the producer runs: a status alert not heard from in
`STALE_FACTOR ×` that becomes its own warning, `<id>.silent` — the gap that
let agent-memory-healthcheck fail for a week in silence.

**inbox.org stays the record.** A raise files one TODO carrying an
`:ALERT_ID:` property (deduped by that property, not by heading text); a clear
appends `Cleared <time>` and, for a routine alert — never acked, never at
`needs` — marks it DONE (decided 24 Sep 2026). `MEDIA_ALERTS_INBOX` points it
elsewhere, `0` turns it off; a host with no ~/org files nothing.

Delivery is the caller's for now: the answer says `notify` and the change, and
`agent-alert` (agent-config) hands it to the digest pane as before. Step 2 of
the proposal puts an `alerts` event on `/sessions/events` for Next.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from pathlib import Path

LEVELS = ("ok", "info", "warn", "needs")
RANK = {lv: i for i, lv in enumerate(LEVELS)}
WARN = RANK["warn"]
KINDS = ("status", "digest")
ID_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,119}")
#: A status alert unheard from for this many `every_s` is reported silent.
STALE_FACTOR = 3
#: How long a cleared alert or an old digest stays in the list.
KEEP_S = 14 * 86400
_LIMITS = {"title": 200, "detail": 4000, "fix": 1000, "host": 64}

_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'status',
  level TEXT NOT NULL DEFAULT 'ok',
  peak TEXT NOT NULL DEFAULT 'ok',
  step INTEGER NOT NULL DEFAULT 0,
  pending_level TEXT,
  pending_count INTEGER NOT NULL DEFAULT 0,
  title TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  fix TEXT NOT NULL DEFAULT '',
  host TEXT NOT NULL DEFAULT '',
  every_s INTEGER,
  first_seen REAL,
  last_seen REAL NOT NULL,
  changed_at REAL NOT NULL,
  cleared_at REAL,
  acked_at REAL
);
CREATE TABLE IF NOT EXISTS alert_log (
  at REAL NOT NULL,
  id TEXT NOT NULL,
  change TEXT NOT NULL,
  level TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS alert_log_at ON alert_log(at);
"""


def _db_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    d = root / "agent-media"
    d.mkdir(parents=True, exist_ok=True)
    return d / "alerts.db"


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path(), timeout=5)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def _clip(body: dict, key: str) -> str:
    return str(body.get(key) or "").strip()[:_LIMITS[key]]


def _int(v, lo: int, hi: int, default: int | None) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _public(row: sqlite3.Row | dict) -> dict:
    r = dict(row)
    return {k: r.get(k) for k in ("id", "kind", "level", "peak", "step", "title", "detail",
                                  "fix", "host", "every_s", "first_seen", "last_seen",
                                  "changed_at", "cleared_at", "acked_at")} | {
        "open": r.get("kind") == "status" and RANK.get(r.get("level"), 0) >= WARN}


def _log(con, now: float, aid: str, change: str, level: str, title: str) -> None:
    con.execute("INSERT INTO alert_log VALUES (?,?,?,?,?)", (now, aid, change, level, title))


# --- reporting ---------------------------------------------------------------------

def report(body: dict, *, now: float | None = None) -> tuple[bool, dict]:
    """One producer's report. `(True, {"alert", "change", "notify"})`, or
    `(False, {"error", "status"})` for a report that says nothing usable."""
    now = time.time() if now is None else now
    aid = str(body.get("id") or "").strip()
    if not ID_RE.fullmatch(aid):
        return False, {"error": "id: lower-case letters, digits and . _ : - (≤120)",
                       "status": 400}
    level = str(body.get("level") or "").strip()
    if level not in RANK:
        return False, {"error": f"level: one of {', '.join(LEVELS)}", "status": 400}
    kind = str(body.get("kind") or "status").strip()
    if kind not in KINDS:
        return False, {"error": "kind: status or digest", "status": 400}
    fields = {k: _clip(body, k) for k in _LIMITS}
    step = _int(body.get("step"), 0, 1_000_000, 0) or 0
    confirm = _int(body.get("confirm"), 1, 10, 1) or 1
    every_s = _int(body.get("every_s"), 30, 30 * 86400, None)
    with _LOCK:
        con = _connect()
        try:
            with con:
                change, row = _apply(con, aid, kind, level, fields, step, confirm,
                                     every_s, now)
                silent = _sweep(con, now)
        finally:
            con.close()
    for r in silent:
        _org(r, "raised")
    notify = change in ("raised", "escalated") or (
        change == "digest" and RANK[level] >= WARN)
    if change in ("raised", "cleared"):
        _org(row, change)
    return True, {"alert": _public(row), "change": change, "notify": notify}


def _apply(con, aid, kind, level, f, step, confirm, every_s, now):
    row = con.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
    if row is None:
        con.execute(
            "INSERT INTO alerts (id, kind, level, peak, step, title, detail, fix, host, "
            "every_s, first_seen, last_seen, changed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, kind, "ok", "ok", 0, f["title"], f["detail"], f["fix"], f["host"],
             every_s, None, now, now))
        row = con.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
    r = dict(row)
    # What the producer says about itself is always the latest word — the
    # title and detail of a clear say "back up", of a raise what is wrong.
    r["title"] = f["title"] or r["title"]
    r["host"] = f["host"] or r["host"]
    r["detail"], r["fix"] = f["detail"], f["fix"]
    r["kind"], r["every_s"], r["last_seen"] = kind, every_s, now
    change = None

    if kind == "digest":
        r.update(level=level, peak=level, step=step, changed_at=now, first_seen=now,
                 cleared_at=None, acked_at=None, pending_level=None, pending_count=0)
        change = "digest"
    else:
        old, new = RANK[r["level"]], RANK[level]
        if new == old:
            r["pending_level"], r["pending_count"] = None, 0
            if new >= WARN and step > (r["step"] or 0):
                change = "escalated"
                r["changed_at"] = now
            r["step"] = step
        elif new > old and confirm > 1 and not (old >= WARN):
            # A raise waits for `confirm` reports in a row at that level.
            same = r["pending_level"] == level
            r["pending_count"] = (r["pending_count"] + 1) if same else 1
            r["pending_level"] = level
            if r["pending_count"] >= confirm:
                change = _move(r, level, step, now)
        else:
            change = _move(r, level, step, now)
    if change:
        _log(con, now, aid, change, r["level"], r["title"])
    con.execute(
        "UPDATE alerts SET kind=?, level=?, peak=?, step=?, pending_level=?, pending_count=?,"
        " title=?, detail=?, fix=?, host=?, every_s=?, first_seen=?, last_seen=?,"
        " changed_at=?, cleared_at=?, acked_at=? WHERE id=?",
        (r["kind"], r["level"], r["peak"], r["step"], r["pending_level"], r["pending_count"],
         r["title"], r["detail"], r["fix"], r["host"], r["every_s"], r["first_seen"],
         r["last_seen"], r["changed_at"], r["cleared_at"], r["acked_at"], aid))
    if not aid.endswith(".silent"):
        _unsilence(con, aid, now)
    return change, r


def _move(r: dict, level: str, step: int, now: float) -> str | None:
    """Change a status row's level; the name of the change."""
    old, new = RANK[r["level"]], RANK[level]
    r.update(level=level, step=step, changed_at=now, pending_level=None, pending_count=0)
    if new >= WARN and old < WARN:
        r.update(first_seen=now, peak=level, cleared_at=None, acked_at=None)
        return "raised"
    if new >= WARN:
        if new > old:
            r["peak"] = level if new > RANK[r["peak"]] else r["peak"]
            return "escalated"
        return "eased"
    if old >= WARN:
        r["cleared_at"] = now
        return "cleared"
    return None


# --- silence -----------------------------------------------------------------------

def _sweep(con, now: float) -> list[dict]:
    """Raise `<id>.silent` for a status alert whose producer stopped reporting;
    the rows it raised, for the inbox."""
    raised = []
    for row in con.execute(
            "SELECT id, title, every_s, last_seen, host FROM alerts WHERE kind='status'"
            " AND every_s IS NOT NULL AND id NOT LIKE '%.silent'").fetchall():
        if now - row["last_seen"] <= STALE_FACTOR * row["every_s"]:
            continue
        sid = row["id"] + ".silent"
        cur = con.execute("SELECT level FROM alerts WHERE id=?", (sid,)).fetchone()
        if cur and RANK[cur["level"]] >= WARN:
            continue
        mins = int((now - row["last_seen"]) // 60)
        change, r = _apply(con, sid, "status", "warn",
               {"title": f"{row['id']} stopped reporting",
                "detail": f"Last heard from {mins} min ago; it reports every "
                          f"{row['every_s']} s. Its timer or script may be broken.",
                "fix": f"Check the watcher behind {row['id']}: systemctl --user list-timers,"
                       " then its journal.",
                "host": row["host"]}, 0, 1, None, now)
        if change == "raised":
            raised.append(r)
    return raised


def _unsilence(con, aid: str, now: float) -> None:
    sid = aid + ".silent"
    cur = con.execute("SELECT level FROM alerts WHERE id=?", (sid,)).fetchone()
    if cur and RANK[cur["level"]] >= WARN:
        _apply(con, sid, "status", "ok",
               {"title": f"{aid} is reporting again", "detail": "", "fix": "", "host": ""},
               0, 1, None, now)


# --- reading and acking ----------------------------------------------------------

def listing(*, open_only: bool = False, now: float | None = None) -> dict:
    """Open alerts first (worst, then newest), then digests and recent clears."""
    now = time.time() if now is None else now
    with _LOCK:
        con = _connect()
        try:
            with con:
                silent = _sweep(con, now)
                con.execute("DELETE FROM alert_log WHERE at < ?", (now - KEEP_S,))
            rows = [_public(r) for r in con.execute("SELECT * FROM alerts").fetchall()]
        finally:
            con.close()
    for r in silent:
        _org(r, "raised")
    open_ = sorted((r for r in rows if r["open"]),
                   key=lambda r: (-RANK[r["level"]], -r["changed_at"]))
    if open_only:
        return {"alerts": open_, "at": now}
    rest = sorted((r for r in rows if not r["open"] and now - r["changed_at"] < KEEP_S
                   and (r["kind"] == "digest" or r["cleared_at"])),
                  key=lambda r: -r["changed_at"])
    return {"alerts": open_ + rest, "at": now}


def ack(aid: str, *, now: float | None = None) -> tuple[bool, dict]:
    now = time.time() if now is None else now
    with _LOCK:
        con = _connect()
        try:
            with con:
                row = con.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
                if row is None:
                    return False, {"error": "no such alert", "status": 404}
                con.execute("UPDATE alerts SET acked_at=? WHERE id=?", (now, aid))
                _log(con, now, aid, "acked", row["level"], row["title"])
                row = con.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
        finally:
            con.close()
    return True, {"alert": _public(row)}


# --- the inbox record ------------------------------------------------------------

def _inbox() -> Path | None:
    raw = os.environ.get("MEDIA_ALERTS_INBOX")
    if raw is not None and raw.strip() in ("", "0", "off"):
        return None
    p = Path(raw).expanduser() if raw else Path.home() / "org" / "inbox.org"
    return p if p.is_file() else None


def _stamp(t: float, *, inactive: bool = True) -> str:
    s = time.strftime("%Y-%m-%d %a %H:%M", time.localtime(t))
    return f"[{s}]" if inactive else s


def _entry_span(lines: list[str], aid: str) -> tuple[int, int] | None:
    """The open `* TODO` entry carrying `:ALERT_ID: aid`: (heading, end)."""
    prop = re.compile(r"^\s*:ALERT_ID:\s+" + re.escape(aid) + r"\s*$")
    head = None
    for i, line in enumerate(lines):
        if re.match(r"^\*+ ", line):
            head = i
        elif head is not None and prop.match(line) and re.match(r"^\*+ TODO ", lines[head]):
            end = next((j for j in range(head + 1, len(lines))
                        if re.match(r"^\*+ ", lines[j])), len(lines))
            return head, end
    return None


def _org(r: dict, change: str) -> None:
    """File the TODO on a raise; note (and for a routine one, close) on a clear.

    Read, change and replace in one go, straight after reading: the file is
    also Emacs's and org-autosync's, so the window for a lost edit is the
    width of this function. A failure here never fails the report.
    """
    path = _inbox()
    if not path:
        return
    try:
        text = path.read_text()
        lines = text.split("\n")
        span = _entry_span(lines, r["id"])
        if change == "raised":
            if span:
                return
            body = [f"* TODO {r['title'] or r['id']}",
                    "  :PROPERTIES:", f"  :ALERT_ID: {r['id']}", "  :END:",
                    f"  Raised {_stamp(r['changed_at'])} by {r['host'] or 'a watcher'}"
                    f" ({r['level']})."]
            body += ["  " + ln for ln in (r["detail"] or "").splitlines()[:12]]
            if r["fix"]:
                body.append(f"  Fix: {r['fix']}")
            new = text.rstrip("\n") + "\n\n" + "\n".join(body) + "\n"
        else:
            if not span:
                return
            head, end = span
            while end > head + 1 and not lines[end - 1].strip():
                end -= 1
            note = f"  Cleared {_stamp(r['cleared_at'] or time.time())}."
            routine = not r.get("acked_at") and r.get("peak") != "needs"
            lines.insert(end, note)
            if routine:
                lines[head] = re.sub(r"^(\*+) TODO ", r"\1 DONE ", lines[head], count=1)
                lines.insert(head + 1, f"  CLOSED: {_stamp(r['cleared_at'] or time.time())}")
            new = "\n".join(lines)
        tmp = path.with_name(f".{path.name}.alerts.tmp")
        tmp.write_text(new)
        os.replace(tmp, path)
    except OSError:
        pass


def _reset_for_tests() -> None:
    """Nothing is cached in-process; kept for conftest's symmetry."""
