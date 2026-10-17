"""Search: every thread's messages, titles, recaps and projects, and — when
agent-memory is installed here — long-term memory beside them.

    GET /search?q=<words>[&limit=][&before=<at>][&tools=1][&memory=0]

**The index** is a SQLite FTS5 database in the state dir (`search.db`,
`XDG_STATE_HOME/agent-media/`). It is fed by the same parser the thread log
uses (`transcript.Builder`), so a hit is a message the app can open, by the
same id. What is indexed, per harness:

- **Claude Code** — `~/.claude/projects/*/<session>.jsonl` (`CLAUDE_CONFIG_DIR`):
  every message the log shows. Sidechains (a subagent's own turns) are not
  messages and are not indexed; the subagent's files under `<session>/` are
  not read.
- **pi** — `~/.pi/agent/sessions/*/*.jsonl`: each `message` record.
- **Codex** — `~/.codex/sessions/Y/M/D/rollout-*.jsonl`: each `message`
  response item (the preamble Codex staples in is skipped), and its tool calls.
- **Hermes** — every profile's `state.db`, `messages` table, read-only.
- **Matrix rooms** (17 Oct 2026) — every room matrix.py serves (the allow
  list, `MATRIX_ROOM_ALLOW`, and the owner's bridge rooms), once loaded:
  what its `Room` holds — the newest page, the sync's events since, pages
  scrolled back to — by event id, the id `/conversation/log` gives it.
  Indexed when the room's `rev` moves; an edit takes the new words, a
  message taken back is dropped (`Room.amends`), and rows are never
  dropped just because the room no longer holds them. Older history is
  paged in by `backfill` from the background thread only — `BACKFILL_PAGES`
  pages of `matrix.PAGE` a pass, outside the writer lock, down to the start
  of each room (its cursor is the room's `files` row, `matrix:<room id>`),
  and first any gap left below what a room holds when it is read again
  after a restart. A query never waits on the homeserver: it indexes only
  what is in memory, and passes over a room whose lock a read holds. Only
  in the canvas's process: `python -m … rebuild` from a shell has no rooms
  (the canvas reads them again on its next pass).

Text (the words of both sides, the narration, an ask's question and answer)
goes into one full-text table; tool steps (name, title, input and result
summaries — never file contents) into another, searched only with `tools=1`.

**Incremental.** Per file: inode, size, mtime, the offset read to, the bytes
just before it (a rewrite in place is a new file) and `resume` — the offset of
the last prompt read. A file that grew is read again from `resume`: the rows
from there on are deleted and rebuilt, because the turn that was open when it
was last read has grown. An unchanged file is a stat. A Hermes store resumes
from the highest message id it has seen.

**When.** A background thread (`start`, from the canvas's `main`) builds the
index a file at a time at a low thread priority, then looks for changed files
every `MEDIA_SEARCH_INTERVAL_S` (60 s). A query first brings changed files up
to date itself, within `QUERY_BUDGET_S`, so a reply written seconds ago is
found. `MEDIA_SEARCH_INDEX=0` stops the background thread (queries still
index lazily). `python -m agent_media_server.search rebuild` starts over;
`status` prints the counts.

**Left out.** Threads in an excluded folder (Meridian's pool:
`sessions._excluded_dirs`, MEDIA_SESSIONS_EXCLUDE_CWD) are never read.
Machinery — a `claude -p` run nobody chats in (`entrypoint` `sdk-*`: the
slash-menu probe, a pipeline's calls) — is indexed but never answered,
unless sessiond holds it as a headless thread (§17). Deleted and sealed
threads are neither read nor answered; a Matrix room is answered only while
matrix.py serves it (one taken off the allow list keeps its rows, unanswered).

Read-only toward every transcript; the only file written is the index.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

from . import auth, transcript

#: Bump when the schema or what a row holds changes: the index is rebuilt.
SCHEMA_VERSION = 1
#: A query brings changed files up to date first, for at most this long.
QUERY_BUDGET_S = 0.6
#: How often the background thread looks for changed files.
INTERVAL_S = 60.0
#: Results a query answers by default, and at most.
LIMIT = 20
LIMIT_MAX = 100
#: Thread hits (title / recap / project) at most.
THREADS_MAX = 20
#: Characters of a message body indexed. A reply is rarely a tenth of it.
BODY_MAX = 64 * 1024
#: Match markers inside snippets, turned into offsets before they leave.
_OPEN, _CLOSE = "\x02", "\x03"
#: Tokens of context in a snippet.
SNIPPET_TOKENS = 14

_CLAUDE_META = (b'"ai-title"', b'"custom-title"', b'away_summary')
_TEXT, _TOOL = 0, 1


# --- the database ---------------------------------------------------------------


def db_path() -> Path:
    from agent_media_core._paths import state_dir

    return state_dir() / "search.db"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, harness TEXT, session TEXT, ino INTEGER, size INTEGER,
  mtime REAL, offset INTEGER, seam BLOB, resume INTEGER, skip INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS threads (
  session TEXT PRIMARY KEY, harness TEXT, cwd TEXT, project TEXT, title TEXT,
  ai_title TEXT, first_prompt TEXT, recap TEXT, recap_at REAL, entrypoint TEXT,
  first_at REAL, last_at REAL);
CREATE TABLE IF NOT EXISTS docs (
  id INTEGER PRIMARY KEY, session TEXT, msg TEXT, role TEXT, at REAL,
  pos INTEGER, kind INTEGER, body TEXT);
CREATE INDEX IF NOT EXISTS docs_session_pos ON docs(session, pos);
CREATE VIRTUAL TABLE IF NOT EXISTS text_fts USING fts5(
  body, content='docs', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE IF NOT EXISTS tool_fts USING fts5(
  body, content='docs', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
"""

_LOCAL = threading.local()
#: One writer at a time: the background thread or a query's catch-up.
_WRITE = threading.Lock()


def _connect() -> sqlite3.Connection:
    """This thread's connection to the index (made, and the schema checked,
    on first use; made again when the state dir moves — tests move it)."""
    path = db_path()
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None and getattr(_LOCAL, "path", None) == str(path):
        return conn
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # Small: red5 is RAM-tight, and the index is read a page at a time.
    conn.execute("PRAGMA cache_size=-4000")
    conn.executescript(_SCHEMA)
    got = conn.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
    if got is None or got[0] != str(SCHEMA_VERSION):
        _wipe(conn)
    _LOCAL.conn, _LOCAL.path = conn, str(path)
    return conn


def _wipe(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    for t in ("files", "threads", "docs"):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("INSERT INTO text_fts(text_fts) VALUES('delete-all')")
    conn.execute("INSERT INTO tool_fts(tool_fts) VALUES('delete-all')")
    conn.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (str(SCHEMA_VERSION),))
    conn.execute("COMMIT")
    _MATRIX_REV.clear()
    _MATRIX_GAPS.clear()


def forget(session: str) -> int:
    """Take a session out of the index: its messages, its thread row and its
    files. The number of message rows removed. (A deleted thread's files are
    not read again: `_sources` leaves deleted sessions out.)"""
    with _WRITE:
        conn = _connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            n = conn.execute("SELECT count(*) FROM docs WHERE session=?", (session,)).fetchone()[0]
            _delete_docs(conn, "session=?", (session,))
            conn.execute("DELETE FROM threads WHERE session=?", (session,))
            conn.execute("DELETE FROM files WHERE session=?", (session,))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    _LISTED[0] = (0.0, {})
    _MATRIX_REV.pop(session, None)
    _MATRIX_GAPS.pop(session, None)
    return int(n)


def _reset_for_tests() -> None:
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None:
        conn.close()
    _LOCAL.conn = _LOCAL.path = None
    _MEMORY[0] = (0.0, False)
    _LISTED[0] = (0.0, {})
    _MATRIX_REV.clear()
    _MATRIX_GAPS.clear()


def _delete_docs(conn: sqlite3.Connection, where: str, args: tuple) -> None:
    """Delete docs rows, and their entries in the (external-content) FTS
    tables, which must be told the old text."""
    for rid, kind, body in conn.execute(f"SELECT id, kind, body FROM docs WHERE {where}",
                                        args).fetchall():
        fts = "tool_fts" if kind == _TOOL else "text_fts"
        conn.execute(f"INSERT INTO {fts}({fts}, rowid, body) VALUES('delete', ?, ?)", (rid, body))
    conn.execute(f"DELETE FROM docs WHERE {where}", args)


def _insert_doc(conn: sqlite3.Connection, session: str, msg: str, role: str, at: float,
                pos: int, kind: int, body: str) -> None:
    body = body.strip()[:BODY_MAX]
    if not body:
        return
    cur = conn.execute("INSERT INTO docs(session, msg, role, at, pos, kind, body) "
                       "VALUES(?,?,?,?,?,?,?)", (session, msg, role, at, pos, kind, body))
    fts = "tool_fts" if kind == _TOOL else "text_fts"
    conn.execute(f"INSERT INTO {fts}(rowid, body) VALUES(?, ?)", (cur.lastrowid, body))


# --- what a message says --------------------------------------------------------


def bodies(msg: dict) -> tuple[str, str]:
    """`(text, tools)` of a §6.2.2 message, as indexed: the words — text
    parts (markers out), narration, an ask's questions and its answer — and
    the tool steps, one per line group."""
    text, tools = [], []
    for p in msg.get("parts") or []:
        t = p.get("type")
        if t == "text":
            words = p.get("text") or ""
            text.append(transcript.display_text(words) if "[[" in words else words)
        elif t == "reasoning" and not p.get("redacted"):
            text.append(p.get("text") or "")
        elif t == "ask":
            for q in p.get("ask") or []:
                text.append(str(q.get("question") or ""))
            if p.get("answer"):
                text.append(str(p["answer"]))
        elif t == "tool":
            tools.append("\n".join(s for s in (
                str(p.get("name") or ""), str(p.get("title") or ""),
                str(p.get("input_summary") or ""), str(p.get("result_summary") or ""))
                if s))
    return "\n\n".join(s for s in text if s.strip()), "\n\n".join(tools)


# --- the thread metadata --------------------------------------------------------


def _excluded(cwd: str) -> bool:
    from . import sessions

    if not cwd:
        return False
    real = os.path.realpath(cwd)
    return any(real == d or real.startswith(d + os.sep) for d in sessions._excluded_dirs())


def _project(cwd: str) -> str:
    from . import sessions

    try:
        return sessions.project_of(cwd) or "" if cwd else ""
    except Exception:  # noqa: BLE001 — a name is a nicety, never a failure
        return ""


def _upsert_thread(conn: sqlite3.Connection, session: str, harness: str, meta: dict) -> None:
    """Merge what this pass learned about the thread into its row."""
    row = conn.execute("SELECT cwd, title, ai_title, first_prompt, recap, recap_at, "
                       "entrypoint, first_at, last_at FROM threads WHERE session=?",
                       (session,)).fetchone()
    old = dict(zip(("cwd", "title", "ai_title", "first_prompt", "recap", "recap_at",
                    "entrypoint", "first_at", "last_at"), row)) if row else {}
    # Names: the latest wins. Where it ran, how, and what was asked first:
    # the first one found stays.
    new = {k: (meta.get(k) if meta.get(k) not in (None, "") else old.get(k))
           for k in ("title", "ai_title")}
    new.update({k: (old.get(k) if old.get(k) not in (None, "") else meta.get(k))
                for k in ("cwd", "first_prompt", "entrypoint")})
    if meta.get("recap_at") and (meta["recap_at"] >= (old.get("recap_at") or 0)):
        new["recap"], new["recap_at"] = meta.get("recap"), meta["recap_at"]
    else:
        new["recap"], new["recap_at"] = old.get("recap"), old.get("recap_at")
    firsts = [a for a in (old.get("first_at"), meta.get("first_at")) if a]
    lasts = [a for a in (old.get("last_at"), meta.get("last_at")) if a]
    new["first_at"] = min(firsts) if firsts else None
    new["last_at"] = max(lasts) if lasts else None
    project = _project(new["cwd"] or "")
    conn.execute("INSERT OR REPLACE INTO threads(session, harness, cwd, project, title, "
                  "ai_title, first_prompt, recap, recap_at, entrypoint, first_at, last_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (session, harness, new["cwd"], project, new["title"], new["ai_title"],
                   new["first_prompt"], new["recap"], new["recap_at"], new["entrypoint"],
                   new["first_at"], new["last_at"]))


def _one_line(text: str, n: int = 200) -> str:
    return " ".join(str(text or "").split())[:n]


# --- Claude Code ----------------------------------------------------------------


def _claude_files() -> list[tuple[str, str]]:
    from agent_media_core import harnesses

    root = harnesses._claude_dir() / "projects"
    out = []
    for f in glob.glob(str(root / "*" / "*.jsonl")):
        sid = os.path.basename(f)[:-6]
        if transcript_session(sid):
            out.append((f, sid))
    return out


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def transcript_session(sid: str) -> bool:
    return bool(_UUID.fullmatch(sid))


def _claude_head(path: str) -> tuple[str, str]:
    """`(cwd, entrypoint)` from the first records that carry them."""
    cwd = entry = ""
    try:
        with open(path, "rb") as fh:
            for n, raw in enumerate(fh):
                if n > 400 or (cwd and entry):
                    break
                if b'"cwd"' not in raw and b'"entrypoint"' not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                cwd = cwd or str(rec.get("cwd") or "")
                entry = entry or str(rec.get("entrypoint") or "")
    except OSError:
        pass
    return cwd, entry


def _index_claude(conn: sqlite3.Connection, path: str, session: str, st: os.stat_result,
                  row: tuple | None) -> None:
    """Bring one Claude Code transcript's rows up to date (see the module
    docstring's "Incremental")."""
    with open(path, "rb") as fh:
        end = transcript._complete_end(fh, st.st_size)
        prev_offset = row[0] if row else 0
        appended = bool(row) and row[3] == st.st_ino and 0 < prev_offset <= end \
            and transcript._seam(fh, prev_offset) == (row[1] or b"")
        meta: dict = {}
        if appended:
            start = row[2] or 0
        else:
            start = 0
            cwd, entry = _claude_head(path)
            if _excluded(cwd):
                conn.execute("BEGIN IMMEDIATE")
                _delete_docs(conn, "session=?", (session,))
                conn.execute("DELETE FROM threads WHERE session=?", (session,))
                _save_file(conn, path, "claude", session, st, end, transcript._seam(fh, end),
                           end, skip=1)
                conn.execute("COMMIT")
                return
            meta.update(cwd=cwd, entrypoint=entry)
        conn.execute("BEGIN IMMEDIATE")
        try:
            if appended:
                _delete_docs(conn, "session=? AND pos>=?", (session, start))
            else:
                _delete_docs(conn, "session=?", (session,))
            resume = _feed_claude(conn, fh, session, start, end, meta)
            _upsert_thread(conn, session, "claude", meta)
            _save_file(conn, path, "claude", session, st, end, transcript._seam(fh, end),
                       resume)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def _feed_claude(conn: sqlite3.Connection, fh, session: str, lo: int, hi: int,
                 meta: dict) -> int:
    """Parse bytes [lo, hi) into messages and write them. Returns the offset
    of the last prompt seen (where the next pass resumes), else `lo`."""
    b = transcript.Builder()
    pos_of: dict[int, int] = {}
    resume = lo

    def flush(upto: int) -> None:
        # Messages before `upto` in b.messages are finished: write, forget.
        for m in b.messages[:upto]:
            text = _write_msg(conn, session, m, pos_of.pop(id(m), resume))
            at = float(m.get("at") or 0)
            if m["role"] == "user" and text and not meta.get("first_prompt"):
                meta["first_prompt"] = _one_line(text)
            if at:
                meta["first_at"] = min(meta.get("first_at") or at, at)
                meta["last_at"] = max(meta.get("last_at") or 0, at)
        del b.messages[:upto]

    fh.seek(lo)
    left = hi - lo
    carry = b""
    offset = lo
    while left > 0 or carry:
        chunk = fh.read(min(1 << 20, left)) if left > 0 else b""
        left -= len(chunk)
        buf = carry + chunk
        lines = buf.split(b"\n")
        carry = lines.pop() if left > 0 else b""
        for raw in lines:
            here = offset
            offset += len(raw) + 1
            if any(k in raw for k in _CLAUDE_META):
                _claude_meta(raw, meta)
            rec = transcript._parse_line(raw)
            if rec is None:
                continue
            n = len(b.messages)
            b.feed(rec)
            if len(b.messages) > n:
                m = b.messages[-1]
                pos_of[id(m)] = here
                if m["role"] == "user":
                    resume = here
                    flush(len(b.messages) - 1)
        if not chunk and not carry:
            break
    b.settle()
    flush(len(b.messages))
    return resume


def _write_msg(conn: sqlite3.Connection, session: str, m: dict, pos: int) -> str:
    """Write one message's rows; its words back, for the thread's metadata."""
    text, tools = bodies(m)
    at = float(m.get("at") or 0)
    _insert_doc(conn, session, m["id"], m["role"], at, pos, _TEXT, text)
    _insert_doc(conn, session, m["id"], m["role"], at, pos, _TOOL, tools)
    return text


def _claude_meta(raw: bytes, meta: dict) -> None:
    try:
        rec = json.loads(raw)
    except ValueError:
        return
    kind = rec.get("type")
    if kind == "ai-title" and rec.get("aiTitle"):
        meta["ai_title"] = _one_line(rec["aiTitle"])
    elif kind == "custom-title" and rec.get("customTitle"):
        meta["title"] = _one_line(rec["customTitle"])
    elif kind == "system" and rec.get("subtype") == "away_summary" and not rec.get("isSidechain"):
        text = rec.get("content")
        if isinstance(text, str) and text.strip():
            meta["recap"] = re.sub(r"\s*\([^()]*\brecaps?\b[^()]*\)\s*$", "", text.strip(),
                                   flags=re.I)
            meta["recap_at"] = transcript.epoch(rec.get("timestamp")) or time.time()


def _save_file(conn, path, harness, session, st, offset, seam, resume, skip=0) -> None:
    conn.execute("INSERT OR REPLACE INTO files(path, harness, session, ino, size, mtime, "
                 "offset, seam, resume, skip) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (path, harness, session, st.st_ino, st.st_size, st.st_mtime, offset, seam,
                  resume, skip))


# --- pi and Codex: one record, one message --------------------------------------


def _pi_files() -> list[tuple[str, str]]:
    from agent_media_core import harnesses

    out = []
    for f in glob.glob(str(harnesses._pi_dir() / "sessions" / "*" / "*.jsonl")):
        m = _UUID.search(os.path.basename(f))
        if m:
            out.append((f, m.group(0)))
    return out


def _codex_files() -> list[tuple[str, str]]:
    from agent_media_core import harnesses

    out = []
    for f in glob.glob(str(harnesses._codex_dir() / "sessions" / "*" / "*" / "*" / "rollout-*.jsonl")):
        m = harnesses._ROLLOUT.search(f)
        if m and not harnesses.codex_subagent(f) and not harnesses.codex_scripted(f):
            out.append((f, m.group(1)))
    return out


def _pi_record(rec: dict, meta: dict) -> tuple[str, str, str] | None:
    """`(role, text, tools)` of one pi record, or None."""
    kind = rec.get("type")
    if kind == "session":
        meta["cwd"] = meta.get("cwd") or str(rec.get("cwd") or "")
        return None
    if kind == "session_info" and rec.get("name"):
        meta["title"] = _one_line(rec["name"])
        return None
    if kind != "message":
        return None
    msg = rec.get("message") or {}
    role = msg.get("role")
    content = msg.get("content")
    parts = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
    text, tools = [], []
    for p in parts:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            text.append(str(p.get("text") or ""))
        elif p.get("type") == "thinking" and p.get("thinking"):
            text.append(str(p["thinking"]))
        elif p.get("type") == "toolCall":
            args = p.get("arguments")
            tools.append(f"{p.get('name') or ''}\n{_one_line(json.dumps(args) if args else '', 300)}")
    if role == "toolResult":
        return "assistant", "", _one_line("\n".join(text), 300)
    if role not in ("user", "assistant"):
        return None
    return role, "\n\n".join(text), "\n\n".join(tools)


def _codex_record(rec: dict, meta: dict) -> tuple[str, str, str] | None:
    from agent_media_core import harnesses

    p = rec.get("payload") or {}
    if rec.get("type") == "session_meta":
        meta["cwd"] = meta.get("cwd") or str(p.get("cwd") or "")
        return None
    if rec.get("type") != "response_item":
        return None
    kind = p.get("type")
    if kind == "message" and p.get("role") in ("user", "assistant"):
        content = p.get("content")
        text = content if isinstance(content, str) else "".join(
            str(c.get("text") or "") for c in content or []
            if isinstance(c, dict) and c.get("type") in ("text", "input_text", "output_text"))
        if p["role"] == "user" and harnesses._is_preamble(text):
            return None
        return p["role"], text, ""
    if kind in ("function_call", "custom_tool_call", "local_shell_call"):
        args = p.get("arguments") or p.get("input") or p.get("action") or ""
        return "assistant", "", f"{p.get('name') or kind}\n" + _one_line(
            args if isinstance(args, str) else json.dumps(args), 300)
    if kind in ("function_call_output", "custom_tool_call_output"):
        out = p.get("output")
        return "assistant", "", _one_line(out if isinstance(out, str) else json.dumps(out), 300)
    return None


def _index_lines(conn: sqlite3.Connection, harness: str, path: str, session: str,
                 st: os.stat_result, row: tuple | None) -> None:
    reader = _pi_record if harness == "pi" else _codex_record
    with open(path, "rb") as fh:
        end = transcript._complete_end(fh, st.st_size)
        prev = row[0] if row else 0
        appended = bool(row) and row[3] == st.st_ino and 0 < prev <= end \
            and transcript._seam(fh, prev) == (row[1] or b"")
        start = prev if appended else 0
        meta: dict = {}
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not appended:
                _delete_docs(conn, "session=?", (session,))
            fh.seek(start)
            offset = start
            for raw in fh.read(end - start).split(b"\n"):
                here = offset
                offset += len(raw) + 1
                if not raw.strip():
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                got = reader(rec, meta)
                if got is None:
                    continue
                role, text, tools = got
                at = transcript.epoch(rec.get("timestamp")) or 0.0
                msg = str(rec.get("id") or "") if harness == "pi" else ""
                msg = msg or f"rec:{here}"
                if text.strip() and role == "user" and not meta.get("first_prompt"):
                    meta["first_prompt"] = _one_line(text)
                meta["first_at"] = min(meta.get("first_at") or at, at) if at else meta.get("first_at")
                meta["last_at"] = max(meta.get("last_at") or 0, at)
                _insert_doc(conn, session, msg, role, at, here, _TEXT, text)
                _insert_doc(conn, session, msg, role, at, here, _TOOL, tools)
            skip = 1 if _excluded(meta.get("cwd") or "") else 0
            if skip:
                _delete_docs(conn, "session=?", (session,))
                conn.execute("DELETE FROM threads WHERE session=?", (session,))
            else:
                _upsert_thread(conn, session, harness, meta)
            _save_file(conn, path, harness, session, st, end, transcript._seam(fh, end), end,
                       skip=skip)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


# --- Hermes ---------------------------------------------------------------------


def _index_hermes(conn: sqlite3.Connection, store: Path, st: os.stat_result,
                  row: tuple | None) -> None:
    """New rows of one Hermes store (`messages.id` above the last seen)."""
    last = int(row[2] or 0) if row else 0
    try:
        src = sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return
    try:
        sess = {r[0]: r[1:] for r in src.execute(
            "SELECT id, cwd, title, started_at FROM sessions")}
        rows = src.execute("SELECT id, session_id, role, content, tool_name, timestamp "
                           "FROM messages WHERE id > ? ORDER BY id", (last,)).fetchall()
    except sqlite3.Error:
        rows, sess = [], {}
    finally:
        src.close()
    conn.execute("BEGIN IMMEDIATE")
    try:
        metas: dict[str, dict] = {}
        for mid, sid, role, content, tool, ts in rows:
            last = max(last, int(mid))
            cwd, title, started = sess.get(sid, ("", "", None))
            if _excluded(cwd or ""):
                continue
            meta = metas.setdefault(sid, {"cwd": cwd or "", "title": _one_line(title or ""),
                                          "first_at": started})
            text = str(content or "")
            at = float(ts or 0)
            meta["last_at"] = max(meta.get("last_at") or 0, at)
            if role == "user" and text.strip() and not meta.get("first_prompt"):
                meta["first_prompt"] = _one_line(text)
            if role == "tool":
                _insert_doc(conn, sid, f"hermes:{mid}", "assistant", at, int(mid), _TOOL,
                            f"{tool or ''}\n{_one_line(text, 300)}")
            elif role in ("user", "assistant"):
                _insert_doc(conn, sid, f"hermes:{mid}", role, at, int(mid), _TEXT, text)
        for sid, meta in metas.items():
            _upsert_thread(conn, sid, "hermes", meta)
        _save_file(conn, str(store), "hermes", "", st, st.st_size, b"", last)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# --- Matrix rooms ---------------------------------------------------------------
#
# A room has no file: its messages are what `matrix.Room` holds (the newest
# page, the sync's events since, pages the app scrolled back to). They are
# indexed from there, by event id, whenever the room's `rev` moved; history
# older than any of that is paged in by `backfill`, from the background
# thread only. Rows are never dropped because a room no longer holds them —
# only when the room says a message was taken back (`Room.amends`).

#: thread → the `Room.rev` last indexed, this process.
_MATRIX_REV: dict[str, int] = {}
#: thread → the /messages token below the held messages, where a gap may
#: be (the room was read again after a restart and holds nothing indexed).
_MATRIX_GAPS: dict[str, str] = {}
#: History pages (`matrix.PAGE` messages each) fetched per background pass.
BACKFILL_PAGES = 10


def _matrix_rooms() -> list:
    """The rooms matrix.py serves (the allow list, and the owner's bridge
    rooms) that are loaded and neither deleted nor sealed."""
    from agent_media_core.deleted import is_deleted
    from agent_media_core.sealed import is_sealed

    from . import matrix

    return [r for r in list(matrix._ROOMS.values())
            if r.loaded and not is_deleted(r.thread) and not is_sealed(r.thread)]


def _room_served(session: str) -> bool:
    from . import matrix

    return session in matrix._ROOMS


def _matrix_key(r) -> str:
    return f"matrix:{r.id}"


def _indexed(conn: sqlite3.Connection, session: str) -> dict[str, str]:
    return {m: b for m, b in conn.execute(
        f"SELECT msg, body FROM docs WHERE session=? AND kind={_TEXT}", (session,))}


def _put_room_msg(conn: sqlite3.Connection, session: str, msg: str, role: str, at: float,
                  text: str) -> None:
    _delete_docs(conn, "session=? AND msg=?", (session, msg))
    _insert_doc(conn, session, msg, role, at, int(at * 1000), _TEXT, text)


def _index_room(conn: sqlite3.Connection, r, blocking: bool) -> bool | None:
    """Bring one room's rows up to what it holds. True when something was
    written, False when nothing changed, None when the room was busy (a
    homeserver read holds its lock) and was left for the next pass."""
    if not r.lock.acquire(timeout=5.0 if blocking else 0.05):
        return None
    try:
        first = r.thread not in _MATRIX_REV
        if not first and _MATRIX_REV[r.thread] == r.rev and not r.amends:
            return False
        rev = r.rev
        held = [(str(m["id"]), m["role"], float(m.get("at") or 0),
                 bodies(m)[0].strip()[:BODY_MAX]) for m in r.messages]
        amends = dict(r.amends)
        title, back = r.title(), r.back
    finally:
        r.lock.release()
    conn.execute("BEGIN IMMEDIATE")
    try:
        have = _indexed(conn, r.thread)
        if first and have and held and back and not any(h[0] in have for h in held):
            # Read again, and nothing it holds is indexed: what came between
            # is paged in by backfill, down to a message that is.
            _MATRIX_GAPS[r.thread] = back
        for msg, role, at, text in held:
            if have.get(msg) != text:
                _put_room_msg(conn, r.thread, msg, role, at, text)
        for msg, parts in amends.items():
            if parts is None:
                _delete_docs(conn, "session=? AND msg=?", (r.thread, msg))
            elif msg in have:
                row = conn.execute("SELECT role, at FROM docs WHERE session=? AND msg=?",
                                   (r.thread, msg)).fetchone()
                text = bodies({"parts": parts})[0]
                _put_room_msg(conn, r.thread, msg, row[0], row[1], text)
        ats = [h[2] for h in held if h[2]]
        _upsert_thread(conn, r.thread, "matrix",
                       {"title": _one_line(title), "first_at": min(ats) if ats else None,
                        "last_at": max(ats) if ats else None})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    with r.lock:
        for msg, parts in amends.items():
            if msg in r.amends and r.amends[msg] is parts:
                del r.amends[msg]
    _MATRIX_REV[r.thread] = rev
    return True


def _room_event(r, ev: dict) -> tuple[str, str, float, str, bool] | None:
    """`(event id, role, at, text, edit)` of one event read going back, or
    None. An edit (`m.replace`) is its original's id with the new words —
    seen before the original, going back; the server's bundled latest edit
    (`unsigned.m.relations.m.replace`) wins over an original's own words."""
    content = ev.get("content") or {}
    if ev.get("type") != "m.room.message" or not content:
        return None
    sender = ev.get("sender") or ""
    rel = content.get("m.relates_to") or {}
    at = round((ev.get("origin_server_ts") or 0) / 1000, 3)
    role = "user" if sender == r.owner else "assistant"
    if rel.get("rel_type") == "m.replace":
        new = content.get("m.new_content") or {}
        if not new or not rel.get("event_id"):
            return None
        return rel["event_id"], role, at, bodies({"parts": r._parts(sender, new)})[0], True
    ev_id = ev.get("event_id") or ""
    if not ev_id:
        return None
    edit = (((ev.get("unsigned") or {}).get("m.relations") or {}).get("m.replace") or {})
    new = (edit.get("content") or {}).get("m.new_content") if isinstance(edit, dict) else None
    words = bodies({"parts": r._parts(sender, new or content)})[0]
    return ev_id, role, at, words, False


def _write_page(conn: sqlite3.Connection, r, evs: list, before: set[str] = frozenset()) -> bool:
    """Write one page of history (newest first). A message already indexed
    keeps its words (a later edit's, or the held copy's); its `at` is the
    original's. True when the page holds a message in `before` (what was
    indexed when the gap was found: the gap is closed)."""
    have = _indexed(conn, r.thread)
    met = False
    ats = []
    for ev in evs:
        got = _room_event(r, ev)
        if got is None:
            continue
        msg, role, at, text, edit = got
        met = met or (not edit and msg in before)
        if msg in have:
            if not edit and at:
                conn.execute("UPDATE docs SET at=?, role=?, pos=? WHERE session=? AND msg=?",
                             (at, role, int(at * 1000), r.thread, msg))
                ats.append(at)
            continue
        _put_room_msg(conn, r.thread, msg, role, at, text)
        have[msg] = text
        if at:
            ats.append(at)
    if ats:
        _upsert_thread(conn, r.thread, "matrix", {"first_at": min(ats), "last_at": max(ats)})
    return met


def backfill(pages: int = BACKFILL_PAGES) -> int:
    """Page older history of the rooms into the index, at most `pages`
    pages this pass: first any gap below what a room holds since it was
    read again, then on down from where the last pass stopped, to the start
    of the room. Never on a query's path: each page is a homeserver read,
    made outside the writer lock. The pages fetched."""
    fetched = 0
    for r in _matrix_rooms():
        if fetched >= pages:
            break
        with _WRITE:
            conn = _connect()
            _index_room(conn, r, blocking=True)
            row = conn.execute("SELECT seam, resume FROM files WHERE path=?",
                               (_matrix_key(r),)).fetchone()
            known = set(_indexed(conn, r.thread))
        gap = _MATRIX_GAPS.get(r.thread)
        while gap and fetched < pages:
            got = r._page(gap)
            fetched += 1
            if got is None:
                break
            evs, gap = got
            with _WRITE:
                conn = _connect()
                conn.execute("BEGIN IMMEDIATE")
                try:
                    met = _write_page(conn, r, evs, known)
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            if met or not gap:
                gap = None
            if gap:
                _MATRIX_GAPS[r.thread] = gap
            else:
                _MATRIX_GAPS.pop(r.thread, None)
        if row and row[1]:
            continue                       # the start of the room is indexed
        if row:
            token = (row[0] or b"").decode() or None
        else:
            with r.lock:
                token = r.back
        while fetched < pages:
            evs: list = []
            if token:
                got = r._page(token)
                fetched += 1
                if got is None:
                    break                  # unreadable now: the next pass
                evs, token = got
            with _WRITE:
                conn = _connect()
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if evs:
                        _write_page(conn, r, evs)
                    conn.execute(
                        "INSERT OR REPLACE INTO files(path, harness, session, ino, size, mtime, "
                        "offset, seam, resume, skip) VALUES(?,?,?,0,0,?,0,?,?,0)",
                        (_matrix_key(r), "matrix", r.thread, time.time(),
                         (token or "").encode(), 0 if token else 1))
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            if not token:
                break
    return fetched


# --- the sweep ------------------------------------------------------------------


def _sources() -> list[tuple[str, str, str]]:
    """`[(harness, path, session)]` for every file the index reads."""
    from agent_media_core import harnesses

    out = [("claude", f, s) for f, s in _claude_files()]
    out += [("pi", f, s) for f, s in _pi_files()]
    out += [("codex", f, s) for f, s in _codex_files()]
    out += [("hermes", str(p), "") for p in harnesses.hermes_stores()]
    from agent_media_core.deleted import deleted
    from agent_media_core.private import marked
    from agent_media_core.sealed import sealed

    # A private thread (agent_media_core.private) is not indexed either.
    gone = deleted().keys() | sealed().keys() | set(marked())
    return [src for src in out if src[2] not in gone] if gone else out


def refresh(budget_s: float | None = None, pause_s: float = 0.0) -> dict:
    """Index every file that changed since it was last read — newest first,
    so a budget spends itself where a search is likeliest to look. Returns
    `{"files", "changed", "done", "seconds"}`; `done` is False when the
    budget ran out first. Skips (answering `done: False`) when another
    refresh holds the writer lock and a budget was given."""
    t0 = time.monotonic()
    if not _WRITE.acquire(blocking=budget_s is None):
        return {"files": 0, "changed": 0, "done": False, "seconds": 0.0}
    try:
        conn = _connect()
        known = {r[0]: r[1:] for r in conn.execute(
            "SELECT path, offset, seam, resume, ino, size, mtime, skip FROM files")}
        todo = []
        for harness, path, session in _sources():
            try:
                st = os.stat(path)
            except OSError:
                continue
            k = known.get(path)
            if harness == "hermes":
                # Hermes writes through its WAL; the store's own stat moves
                # only at a checkpoint. Asking it for new rows is one query.
                todo.append((float("inf"), harness, path, session, st, k))
                continue
            if k and (k[3], k[4], k[5]) == (st.st_ino, st.st_size, st.st_mtime):
                continue
            if k and k[6]:
                # An excluded folder's file: note the new size, read nothing.
                with conn:
                    _save_file(conn, path, harness, session, st, k[0], k[1], k[2], skip=1)
                continue
            todo.append((st.st_mtime, harness, path, session, st, k))
        todo.sort(key=lambda t: -t[0])
        # Matrix rooms first: they are in memory, a query or two each. A room
        # whose lock a homeserver read holds is left for the next pass.
        rooms = _matrix_rooms()
        if _MATRIX_REV:
            # Wiped under us (a `rebuild` from the command line): read again.
            there = {r[0] for r in conn.execute(
                "SELECT session FROM threads WHERE harness='matrix'")}
            for sid in [s for s in _MATRIX_REV if s not in there]:
                del _MATRIX_REV[sid]
        rooms_left = 0
        rooms_changed = 0
        for r in rooms:
            if budget_s is not None and time.monotonic() - t0 > budget_s:
                rooms_left += 1
                continue
            try:
                got = _index_room(conn, r, blocking=budget_s is None)
            except sqlite3.Error as e:
                print(f"search: indexing Matrix room {r.id} failed ({e})", file=sys.stderr)
                continue
            rooms_left += got is None
            rooms_changed += bool(got)
        done = 0
        for _mt, harness, path, session, st, k in todo:
            if budget_s is not None and time.monotonic() - t0 > budget_s:
                break
            row = (k[0], k[1], k[2], k[3]) if k else None
            try:
                if harness == "claude":
                    _index_claude(conn, path, session, st, row)
                elif harness == "hermes":
                    _index_hermes(conn, Path(path), st, row)
                else:
                    _index_lines(conn, harness, path, session, st, row)
            except (OSError, sqlite3.Error) as e:
                print(f"search: indexing {path} failed ({e})", file=sys.stderr)
            done += 1
            if pause_s:
                time.sleep(pause_s)
        return {"files": len(known), "changed": done + rooms_changed,
                "done": done == len(todo) and not rooms_left,
                "seconds": round(time.monotonic() - t0, 3)}
    finally:
        _WRITE.release()


def rebuild() -> dict:
    """Start over: every row dropped, every file read again."""
    with _WRITE:
        _wipe(_connect())
    return refresh()


def stats() -> dict:
    conn = _connect()
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    path = db_path()
    size = sum(os.path.getsize(p) for p in (str(path), str(path) + "-wal")
               if os.path.exists(p))
    return {"path": str(path), "bytes": size, "files": one("SELECT count(*) FROM files"),
            "threads": one("SELECT count(*) FROM threads"),
            "messages": one(f"SELECT count(*) FROM docs WHERE kind={_TEXT}"),
            "tool_steps": one(f"SELECT count(*) FROM docs WHERE kind={_TOOL}")}


_STARTED = threading.Event()


def start() -> None:
    """The background indexer, once per process: build, then keep up. At a
    low thread priority, so it only has the CPU nobody else wants."""
    if _STARTED.is_set() or (os.environ.get("MEDIA_SEARCH_INDEX") or "1").strip() == "0":
        return
    _STARTED.set()
    threading.Thread(target=_run, name="search-index", daemon=True).start()


def _run() -> None:
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 15)
    except (AttributeError, OSError):
        pass
    try:
        interval = float(os.environ.get("MEDIA_SEARCH_INTERVAL_S") or INTERVAL_S)
    except ValueError:
        interval = INTERVAL_S
    time.sleep(10)
    first = True
    while True:
        try:
            got = refresh(pause_s=0.005 if first else 0.0)
            if first or got["changed"]:
                print(f"search: indexed {got['changed']} file(s) in {got['seconds']}s",
                      file=sys.stderr)
            first = False
        except Exception as e:  # noqa: BLE001 — never take the canvas down
            print(f"search: refresh failed ({e})", file=sys.stderr)
        try:
            backfill()
        except Exception as e:  # noqa: BLE001
            print(f"search: Matrix history failed ({e})", file=sys.stderr)
        time.sleep(interval)


# --- the query ------------------------------------------------------------------

_WORD = re.compile(r"\w+", re.UNICODE)


def terms_of(q: str) -> list[str]:
    """The words a query is made of, as they are matched: quoted phrases are
    kept whole, everything else is split into words."""
    out = []
    for m in re.finditer(r'"([^"]+)"|(\S+)', q or ""):
        if m.group(1):
            words = _WORD.findall(m.group(1))
            if words:
                out.append(" ".join(words))
        else:
            out.extend(_WORD.findall(m.group(2)))
    return out


def fts_query(terms: list[str]) -> str:
    """Every term must match; the last one as a prefix (as-you-type)."""
    parts = []
    for i, t in enumerate(terms):
        quoted = '"' + t.replace('"', "") + '"'
        if i == len(terms) - 1 and " " not in t:
            quoted += "*"
        parts.append(quoted)
    return " ".join(parts)


def _marked(snippet: str) -> dict:
    """`{"text", "match": [[start, end], …]}` from a snippet with markers."""
    text, match, i = [], [], 0
    start = None
    for ch in snippet:
        if ch == _OPEN:
            start = i
        elif ch == _CLOSE:
            if start is not None and i > start:
                match.append([start, i])
            start = None
        else:
            text.append(ch)
            i += 1
    return {"text": "".join(text), "match": match}


def _mark_terms(text: str, terms: list[str]) -> list[list[int]]:
    """Offsets of each term (case-insensitive, at the start of a word — a
    word or a prefix of one, as FTS matches) in a plain string: for titles,
    recaps and projects, which are not in FTS."""
    spans = []
    for t in terms:
        for m in re.finditer(r"(?<!\w)" + re.escape(t), text, re.IGNORECASE):
            spans.append([m.start(), m.end()])
    spans.sort()
    merged: list[list[int]] = []
    for s in spans:
        if merged and s[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], s[1])
        else:
            merged.append(s)
    return merged


def _hidden(entrypoint: str | None, session: str, owned: set[str],
            harness: str | None = None) -> bool:
    from agent_media_core.deleted import is_deleted
    from agent_media_core.private import hidden

    if is_deleted(session) or hidden(session):
        return True
    if harness == "matrix":
        # A room is answered only while matrix.py serves it: one taken off
        # the allow list (or with the sync off) keeps its rows, unanswered.
        return not _room_served(session)
    return bool(entrypoint) and entrypoint.startswith("sdk") and session not in owned


def _owned_headless() -> set[str]:
    from . import driver

    if not driver.headless_enabled():
        return set()
    try:
        return {str(v.get("session")) for v in driver.headless_driver().rows() if v.get("session")}
    except Exception:  # noqa: BLE001
        return set()


#: `(monotonic at, rows)`: the thread list, kept briefly — search is typed
#: a keystroke at a time, and each list is a /proc sweep and a tmux call.
_LISTED: list[tuple[float, dict]] = [(0.0, {})]
_LISTED_TTL_S = 10.0


def _listed() -> dict[str, dict]:
    """The thread list's own rows (`/targets`), by session: the names, recaps
    and projects the app shows win over what the index recorded."""
    from . import sessions

    at, rows = _LISTED[0]
    if at and time.monotonic() - at < _LISTED_TTL_S:
        return rows
    try:
        rows = {r["session"]: r for r in sessions.sessions_index() if r.get("session")}
    except Exception as e:  # noqa: BLE001 — the index still answers
        print(f"search: thread list unavailable ({e})", file=sys.stderr)
        rows = {}
    _LISTED[0] = (time.monotonic(), rows)
    return rows


def _thread_view(session: str, t: dict | None, listed: dict | None) -> dict:
    t = t or {}
    listed = listed or {}
    recap = (listed.get("recap") or {}).get("text") if listed.get("recap") else t.get("recap")
    title = (listed.get("title") or t.get("title") or t.get("ai_title")
             or t.get("first_prompt") or "")
    title = title if len(title) <= 60 else title[:59] + "…"
    return {"session": session, "title": title,
            "project": listed.get("project") or t.get("project") or None,
            "harness": t.get("harness") or listed.get("harness") or "claude",
            "live": bool(listed.get("live")), "archived": bool(listed.get("archived")),
            "recap": recap or None, "at": t.get("last_at") or listed.get("at")}


def _threads_rows(conn: sqlite3.Connection) -> dict[str, dict]:
    cols = ("session", "harness", "cwd", "project", "title", "ai_title", "first_prompt",
            "recap", "recap_at", "entrypoint", "first_at", "last_at")
    return {r[0]: dict(zip(cols, r)) for r in conn.execute(f"SELECT {', '.join(cols)} FROM threads")}


def _thread_hits(terms: list[str], rows: dict[str, dict], listed: dict[str, dict],
                 owned: set[str]) -> list[dict]:
    hits = []
    for sid in set(rows) | set(listed):
        t = rows.get(sid)
        if t and _hidden(t.get("entrypoint"), sid, owned, t.get("harness")):
            continue
        if t and _excluded(t.get("cwd") or ""):
            continue
        view = _thread_view(sid, t, listed.get(sid))
        fields = {"title": view["title"], "recap": view["recap"] or "",
                  "project": view["project"] or ""}
        hay = " \n".join(fields.values())
        if not all(_mark_terms(hay, [w]) for w in terms):
            continue
        view["match"] = {k: _mark_terms(v, terms) for k, v in fields.items()
                         if v and _mark_terms(v, terms)}
        hits.append(view)
    hits.sort(key=lambda v: -(v["at"] or 0))
    return hits[:THREADS_MAX]


def _message_hits(conn: sqlite3.Connection, query: str, *, tools: bool, before: float | None,
                  limit: int, rows: dict[str, dict], listed: dict[str, dict],
                  owned: set[str]) -> list[dict]:
    out: list[dict] = []
    tables = [("text_fts", "text")] + ([("tool_fts", "tool")] if tools else [])
    for fts, kind in tables:
        cursor = before if before is not None else float("inf")
        batch = max(limit * 2, 40)
        got = 0
        while got < limit:
            found = conn.execute(
                f"SELECT d.session, d.msg, d.role, d.at, "
                f"snippet({fts}, 0, ?, ?, '…', ?) FROM {fts} JOIN docs d ON d.id = {fts}.rowid "
                f"WHERE {fts} MATCH ? AND d.at < ? ORDER BY d.at DESC LIMIT ?",
                (_OPEN, _CLOSE, SNIPPET_TOKENS, query, cursor, batch)).fetchall()
            if not found:
                break
            for sid, msg, role, at, snip in found:
                t = rows.get(sid)
                if t is None or _hidden(t.get("entrypoint"), sid, owned, t.get("harness")) \
                        or _excluded(t.get("cwd") or ""):
                    continue
                view = _thread_view(sid, t, listed.get(sid))
                out.append({"session": sid, "message": msg, "role": role,
                            "at": round(at, 3), "kind": kind, "snippet": _marked(snip),
                            "thread": {k: view[k] for k in
                                       ("title", "project", "harness", "live", "archived")}})
                got += 1
            if len(found) < batch:
                break
            cursor = found[-1][3]
    out.sort(key=lambda r: -r["at"])
    return out[:limit]


# --- memory ---------------------------------------------------------------------

#: `(checked at, available)`: whether agent-memory answers on this host.
_MEMORY: list[tuple[float, bool]] = [(0.0, False)]
_MEMORY_TTL_S = 60.0


def memory_available() -> bool:
    """Is agent-memory installed here, and answering? Installed: its search
    command on PATH, its env file, or its URL in the environment. Answering:
    `GET /health` within 1.5 s. Cached for a minute."""
    import shutil

    at, ok = _MEMORY[0]
    if time.monotonic() - at < _MEMORY_TTL_S and at:
        return ok
    installed = bool(shutil.which("agent-memory-search")
                     or os.path.exists(os.path.expanduser("~/.local/bin/agent-memory-search"))
                     or any(os.path.exists(os.path.expanduser(f)) for f in
                            ("~/.config/hippocampus.env", "~/.config/sacred-brain.env"))
                     or os.environ.get("AGENT_MEMORY_HIPPOCAMPUS_URL")
                     or os.environ.get("HIPPOCAMPUS_URL"))
    ok = False
    if installed:
        from . import org

        got = org._memory_call("GET", "/health", timeout=1.5)
        ok = bool(got) and str(got.get("status", "ok")).lower() == "ok"
    _MEMORY[0] = (time.monotonic(), ok)
    return ok


def _memory_section(q: str, limit: int = 8) -> dict:
    if not memory_available():
        return {"available": False}
    from . import org

    return {"available": True, "items": org._memories(q, limit)}


# --- the route ------------------------------------------------------------------


def search(q: str, bearer: str, *, limit=None, before=None, tools: bool = False,
           memory: bool = True) -> tuple[bool, dict]:
    """`GET /search` (server-contract.md §6.14)."""
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    terms = terms_of(q)
    if not terms:
        return False, {"error": "nothing to search for", "status": 400}
    try:
        n = max(1, min(LIMIT_MAX, int(limit))) if limit not in (None, "") else LIMIT
    except (TypeError, ValueError):
        n = LIMIT
    try:
        cursor = float(before) if before not in (None, "") else None
    except (TypeError, ValueError):
        return False, {"error": "before must be a time (a result's `at`)", "status": 400}
    from concurrent.futures import ThreadPoolExecutor

    from . import doc_read

    with ThreadPoolExecutor(max_workers=2) as pool:
        mem = pool.submit(_memory_section, " ".join(terms)) if memory and cursor is None else None
        # A project's documents (doc_read.py): first page only.
        found_docs = pool.submit(doc_read.search, terms) if cursor is None else None
        caught = refresh(budget_s=QUERY_BUDGET_S)
        conn = _connect()
        rows = _threads_rows(conn)
        listed = _listed()
        owned = _owned_headless()
        query = fts_query(terms)
        try:
            messages = _message_hits(conn, query, tools=tools, before=cursor, limit=n,
                                     rows=rows, listed=listed, owned=owned)
        except sqlite3.OperationalError as e:
            return False, {"error": f"could not search for that ({e})", "status": 400}
        out = {"q": q, "terms": terms, "tools": bool(tools),
               "threads": _thread_hits(terms, rows, listed, owned) if cursor is None else [],
               "messages": messages,
               "next": messages[-1]["at"] if len(messages) >= n else None,
               "indexing": not caught["done"]}
        if found_docs is not None:
            try:
                out["docs"] = found_docs.result(timeout=6)
            except Exception:  # noqa: BLE001
                out["docs"] = []
        # Memory: first page only, and only when asked (`memory=0` skips it).
        if mem is not None:
            try:
                out["memory"] = mem.result(timeout=8)
            except Exception:  # noqa: BLE001
                out["memory"] = {"available": True, "items": [], "error": "memory did not answer"}
    return True, out


# --- the command ----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else "status"
    if cmd == "rebuild":
        t0 = time.monotonic()
        got = rebuild()
        print(json.dumps({**got, "seconds": round(time.monotonic() - t0, 1), **stats()}))
        return 0
    if cmd == "refresh":
        print(json.dumps(refresh()))
        return 0
    if cmd == "status":
        print(json.dumps(stats()))
        return 0
    if cmd == "query" and len(args) > 1:
        conn = _connect()
        rows = _threads_rows(conn)
        terms = terms_of(" ".join(args[1:]))
        hits = _message_hits(conn, fts_query(terms), tools="--tools" in args, before=None,
                             limit=10, rows=rows, listed={}, owned=set())
        for h in hits:
            print(h["at"], h["session"][:8], h["thread"]["title"][:40], "|", h["snippet"]["text"][:120])
        return 0
    print("usage: python -m agent_media_server.search rebuild|refresh|status|query <words>",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
