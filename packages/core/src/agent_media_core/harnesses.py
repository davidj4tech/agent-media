"""Which coding agent a conversation belongs to, and how to reach it.

The phone was built against Claude Code, and it knew where to look because
there was only one place: `~/.claude/projects/*/<id>.jsonl`, a `claude`
process, `claude --resume`. Codex and pi hold conversations too, and their
speech already reaches the library (each hook tags its turns with the
session). What they lacked was everything after that: a title, a live
state, a way back in. This is the table of those differences.

    claude  ~/.claude/projects/<cwd>/<id>.jsonl
            live: its own ~/.claude/sessions/<pid>.json (see claude_sessions)
            resume: claude --resume <id>
    codex   ~/.codex/sessions/YYYY/MM/DD/rollout-<stamp>-<id>.jsonl
            live: a `codex` process holds its rollout file open, so the
            session is read off /proc/<pid>/fd; it only exists once the
            first message has been sent
            resume: codex resume <id>
    hermes  ~/.hermes/state.db (and profiles/<name>/state.db) — a SQLite
            store, not a file per conversation: `sessions` has the id, cwd
            and title, `messages` the turns
            live: a `hermes` process is python running the venv's script, so
            it is found by argv rather than by name, and it holds its
            profile's state.db open — the session is the newest row in that
            database started since the process was
            resume: hermes --resume <id>; its ids are not uuids but
            `<date>_<time>_<hex>`, which is why `_safe` knows two shapes
    pi      ~/.pi/agent/sessions/--<cwd>--/<stamp>_<id>.jsonl
            live: pi retitles its process to `pi` and drops its arguments,
            and appends to its file without holding it, so nothing outside
            says which session a pi is on. The agent-media extension writes
            it down on every session start (`register_pane`).
            resume: pi --session <id>; a fresh one can be given its id up
            front with --session-id, so a phone-started chat knows its uuid
            before anything is said
    opencode  ~/.local/share/opencode/opencode.db (`XDG_DATA_HOME` moves it) —
            SQLite like Hermes: `session` has the id, directory and title,
            `message` and `part` the turns, each a JSON `data` column
            live: an `opencode` process holds the database open; its session
            is the one `--session` named, else the newest in its directory
            started since the process was
            resume: opencode --session <id>; ids are `ses_` and 26 letters
            and digits, the third shape `_safe` knows

pi's model calls often go through Meridian, which runs Claude Code headless
under systemd. Those `claude` processes have no tmux pane and their sessions
are Meridian's, not the conversation's: a pi conversation is known only by
pi's own session id, and nothing here looks at Meridian's.

Standard library only: the hooks import this on every event.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

CLAUDE, CODEX, PI, HERMES, OPENCODE = "claude", "codex", "pi", "hermes", "opencode"
HARNESSES = (CLAUDE, CODEX, PI, HERMES, OPENCODE)

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
#: Hermes numbers its sessions by the clock instead: `20260921_102508_f74b02`.
_HERMES_ID = r"[0-9]{8}_[0-9]{6}_[0-9a-f]{4,}"
#: opencode's: `ses_` and 26 base62 characters, `ses_f2fa343dcffeKPfrR1z5h6u4NN`.
_OPENCODE_ID = r"ses_[0-9A-Za-z]{26}"
#: Any of the shapes, for anything that takes a session id from outside.
SESSION_ID = re.compile(f"(?:{_UUID}|{_HERMES_ID}|{_OPENCODE_ID})")
_ROLLOUT = re.compile(r"rollout-.*-(" + _UUID + r")\.jsonl$")


def _claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def _codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def _pi_dir() -> Path:
    return Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent").expanduser()


def _hermes_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def opencode_db() -> Path:
    """opencode's one database: every project's sessions, and its sign-ins."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base).expanduser() / "opencode" / "opencode.db"


def opencode_rows(sql: str, args: tuple = ()) -> list[tuple]:
    """`sql` against opencode's database, read-only. [] when it is not there
    or will not answer — a schema opencode has moved on from included."""
    import sqlite3

    db = opencode_db()
    if not db.exists():
        return []
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0) as c:
            return c.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def hermes_stores() -> list[Path]:
    """Every Hermes session store: the default one and each profile's.

    A profile is a separate database with its own sessions, so an id has to be
    looked for in all of them — there is no index above them.
    """
    home = _hermes_dir()
    found = [home / "state.db", *sorted(home.glob("profiles/*/state.db"))]
    return [p for p in found if p.exists()]


def _hermes_rows(sql: str, args: tuple = ()) -> list[tuple]:
    """`sql` against every store, read-only, first store with a row winning."""
    import sqlite3

    for db in hermes_stores():
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0) as c:
                rows = c.execute(sql, args).fetchall()
        except sqlite3.Error:
            continue
        if rows:
            return rows
    return []


def _safe(session: str) -> bool:
    return bool(SESSION_ID.fullmatch(session or ""))


def is_hermes(session: str) -> bool:
    """Whether that id is Hermes-shaped. Cheap enough to ask before a query."""
    return bool(re.fullmatch(_HERMES_ID, session or ""))


def is_opencode(session: str) -> bool:
    """Whether that id is opencode-shaped."""
    return bool(re.fullmatch(_OPENCODE_ID, session or ""))


#: The title opencode gives a session until it has named it.
_OPENCODE_UNNAMED = re.compile(r"^New session - \d{4}-\d\d-\d\dT")


def opencode_last_reply(session: str) -> tuple[str, str]:
    """`(message id, text)` of what the agent said since the last prompt —
    every assistant step's text, in order — or ("", "") when it said nothing.

    opencode's plugin hears "idle" but not the words; they are in the
    database by then, so the speech hook reads them here. The id is the
    last step's, which is what a repeat of the same idle is told apart by.
    """
    rows = opencode_rows(
        "select m.id, json_extract(m.data, '$.role'), p.data from message m "
        "left join part p on p.message_id = m.id where m.session_id = ? "
        "order by m.time_created, m.id, p.id", (session,))
    said: list[str] = []
    last = ""
    for mid, role, pdata in rows:
        if role == "user":
            said, last = [], ""
            continue
        if role != "assistant":
            continue
        last = str(mid)
        try:
            p = json.loads(pdata) if pdata else {}
        except ValueError:
            continue
        if isinstance(p, dict) and p.get("type") == "text" and not p.get("synthetic"):
            text = str(p.get("text") or "").strip()
            if text:
                said.append(text)
    return (last, "\n\n".join(said)) if said else ("", "")


def _opencode_session(session: str) -> tuple:
    """`(directory, title)` of an opencode session, or () when it has none."""
    rows = opencode_rows("select directory, title from session where id = ?", (session,))
    return tuple(rows[0]) if rows else ()


# --- where a conversation is written -------------------------------------------


def transcript(session: str) -> Optional[tuple[str, Path]]:
    """`(harness, path)` of the file this session is written to, or None."""
    if not _safe(session):
        return None
    for harness, pattern in (
            (CLAUDE, _claude_dir() / "projects" / "*" / f"{session}.jsonl"),
            (CODEX, _codex_dir() / "sessions" / "*" / "*" / "*" / f"rollout-*-{session}.jsonl"),
            (PI, _pi_dir() / "sessions" / "*" / f"*_{session}.jsonl")):
        hits = glob.glob(str(pattern))
        if hits:
            return harness, Path(max(hits, key=lambda p: os.path.getmtime(p)))
    return None


def harness_of(session: str) -> str:
    """"claude", "codex", "pi", "hermes", "opencode", or "" when none has it."""
    if is_opencode(session):
        return OPENCODE if _opencode_session(session) else ""
    if is_hermes(session):
        return HERMES if _hermes_rows(
            "select 1 from sessions where id = ?", (session,)) else ""
    found = transcript(session)
    return found[0] if found else ""


def _records(path: Path):
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def cwd_of(session: str) -> str:
    """The directory the session ran in, from its own file. "" if unknown."""
    if is_opencode(session):
        found = _opencode_session(session)
        return str(found[0] or "") if found else ""
    if is_hermes(session):
        rows = _hermes_rows("select cwd from sessions where id = ?", (session,))
        # A Hermes TUI session records no cwd at all; the caller falls back to
        # the pane's own directory rather than guessing one here.
        return str(rows[0][0] or "") if rows else ""
    found = transcript(session)
    if not found:
        return ""
    harness, path = found
    for rec in _records(path):
        if harness == CODEX:
            cwd = (rec.get("payload") or {}).get("cwd") if rec.get("type") == "session_meta" else ""
        else:
            cwd = rec.get("cwd") or ""
        if cwd:
            return str(cwd)
    return ""


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text") or "") for p in content
                       if isinstance(p, dict) and p.get("type") in ("text", "input_text"))
    return ""


def _is_preamble(text: str) -> bool:
    """Codex staples its instructions and environment in as user messages."""
    t = text.lstrip()
    return not t or t.startswith("<") or t.startswith("# AGENTS.md")


def first_prompt(session: str) -> str:
    """The first thing the person asked, for codex, pi, hermes and opencode.
    "" otherwise."""
    if is_opencode(session):
        # A user message's words are its text parts; `synthetic` ones are
        # opencode's own additions (a file it attached), not what was typed.
        rows = opencode_rows(
            "select p.data from part p join message m on m.id = p.message_id "
            "where p.session_id = ? and json_extract(m.data, '$.role') = 'user' "
            "and json_extract(p.data, '$.type') = 'text' "
            "and coalesce(json_extract(p.data, '$.synthetic'), 0) = 0 "
            "order by m.time_created, p.id limit 1", (session,))
        try:
            return " ".join(str(json.loads(rows[0][0]).get("text") or "").split()) if rows else ""
        except (ValueError, AttributeError):
            return ""
    if is_hermes(session):
        rows = _hermes_rows(
            "select content from messages where session_id = ? and role = 'user' "
            "order by id limit 1", (session,))
        return " ".join(str(rows[0][0] or "").split()) if rows else ""
    found = transcript(session)
    if not found or found[0] == CLAUDE:
        return ""
    harness, path = found
    for rec in _records(path):
        if harness == CODEX:
            p = rec.get("payload") or {}
            if rec.get("type") == "response_item" and p.get("type") == "message" \
                    and p.get("role") == "user":
                text = _text_of(p.get("content"))
                if not _is_preamble(text):
                    return " ".join(text.split())
        elif rec.get("type") == "message" and (rec.get("message") or {}).get("role") == "user":
            text = _text_of((rec.get("message") or {}).get("content"))
            if text.strip():
                return " ".join(text.split())
    return ""


def title_of(session: str) -> str:
    """The name the agent gave the conversation, for codex and pi. "" if none.

    Codex names threads in `session_index.jsonl` (rewritten by appending, so
    the last line for an id wins); pi writes `session_info` records into the
    session file (the last one wins, as a /name renames it). Claude's titles
    are read by session_feed, which knows its record types. opencode keeps
    one on the session row: a timestamp until it has thought of a better one.
    """
    if is_opencode(session):
        found = _opencode_session(session)
        title = " ".join(str(found[1] or "").split()) if found else ""
        return "" if _OPENCODE_UNNAMED.match(title) else title
    if is_hermes(session):
        # Hermes leaves it null unless `hermes sessions rename` has been run.
        rows = _hermes_rows("select title from sessions where id = ?", (session,))
        return " ".join(str(rows[0][0] or "").split()) if rows else ""
    found = transcript(session)
    if not found or found[0] == CLAUDE:
        return ""
    harness, path = found
    name = ""
    if harness == CODEX:
        for rec in _records(_codex_dir() / "session_index.jsonl"):
            if rec.get("id") == session and rec.get("thread_name"):
                name = str(rec["thread_name"])
    else:
        for rec in _records(path):
            if rec.get("type") == "session_info" and rec.get("name"):
                name = str(rec["name"])
    return " ".join(name.split())


# --- which are running, and where ----------------------------------------------


@dataclass(frozen=True)
class Running:
    pid: int
    session: str
    pane: str
    harness: str


def _argv(pid: str) -> list[str]:
    try:
        return [a.decode(errors="replace")
                for a in Path("/proc", pid, "cmdline").read_bytes().split(b"\0") if a]
    except OSError:
        return []


def _env_of(pid: str, key: str) -> str:
    """One environment variable of a live process, or ""."""
    want = key.encode() + b"="
    try:
        env = Path("/proc", pid, "environ").read_bytes().split(b"\0")
    except OSError:
        return ""
    return next((e[len(want):].decode(errors="replace") for e in env
                 if e.startswith(want)), "")


def _pane_of(pid: str) -> str:
    try:
        env = Path("/proc", pid, "environ").read_bytes().split(b"\0")
    except OSError:
        return ""
    return next((e[len(b"TMUX_PANE="):].decode(errors="replace")
                 for e in env if e.startswith(b"TMUX_PANE=")), "")


def _codex_session(pid: str) -> str:
    """The rollout a codex process is writing, by the file it holds open."""
    best, best_at = "", -1.0
    for fd in glob.glob(f"/proc/{pid}/fd/*"):
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        m = _ROLLOUT.search(target)
        if not m:
            continue
        try:
            at = os.path.getmtime(target)
        except OSError:
            at = 0.0
        if at > best_at:
            best, best_at = m.group(1), at
    return best


def _started_at(pid: str) -> float:
    """When that process began, near enough to compare sessions against."""
    try:
        return os.path.getmtime(f"/proc/{pid}")
    except OSError:
        return 0.0


def _hermes_pid(argv: list[str]) -> bool:
    """Whether this argv is a Hermes.

    It is a console script: the process is the venv's python and Hermes is
    its first argument, so the name in `comm` is `python3` and the usual
    basename test never matches.
    """
    if not argv:
        return False
    if os.path.basename(argv[0]) == HERMES:
        return True
    return len(argv) > 1 and os.path.basename(argv[1]) == HERMES


def _hermes_store_of(pid: str) -> Optional[Path]:
    """The state.db a running Hermes writes to — which profile it is on.

    Its SQLite connection is opened per transaction, so unlike codex there is
    no held file handle to read it off /proc with. What the process does carry
    is the profile it was started with; failing that, the profile the whole
    installation is switched to (`~/.hermes/active_profile`), which is what a
    `hermes` typed at a prompt gets.
    """
    home = _hermes_dir()
    name = _env_of(pid, "HERMES_PROFILE")
    where = _env_of(pid, "HERMES_HOME")
    if where:
        db = Path(where).expanduser() / "state.db"
        if db.exists():
            return db
    if not name:
        try:
            name = (home / "active_profile").read_text().strip()
        except OSError:
            name = ""
    if name:
        db = home / "profiles" / name / "state.db"
        if db.exists():
            return db
    db = home / "state.db"
    return db if db.exists() else None


def _hermes_session(pid: str) -> str:
    """The session a live Hermes is on, or "" before it has been asked anything.

    Nothing outside the process says which session it holds — the store is
    shared by every Hermes on that profile. What is true is that its session
    was created after the process was, so the newest row started since then,
    in the database this process has open, is the one. A Hermes that has said
    nothing yet has no row at all, which is the same "not yet" codex gives.
    """
    import sqlite3

    db = _hermes_store_of(pid)
    if not db:
        return ""
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0) as c:
            row = c.execute(
                "select id from sessions where started_at >= ? "
                "order by started_at desc limit 1",
                (_started_at(pid) - 5.0,)).fetchone()
    except sqlite3.Error:
        return ""
    return str(row[0]) if row else ""


def _opencode_live_session(pid: str, argv: list[str]) -> str:
    """The session a live opencode is on, or "" before it has one.

    One started with `--session <id>` (a resume, or `-s`) says so in its
    arguments. Otherwise it is like Hermes: the database is shared by every
    opencode, so the session is the newest one in this process's directory
    started since the process was. A subagent's session has a parent and is
    never the one the pane is on.
    """
    for i, a in enumerate(argv[1:], 1):
        if a in ("--session", "-s") and i + 1 < len(argv) and is_opencode(argv[i + 1]):
            return argv[i + 1]
        if a.startswith("--session=") and is_opencode(a.split("=", 1)[1]):
            return a.split("=", 1)[1]
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""
    rows = opencode_rows(
        "select id from session where directory = ? and parent_id is null "
        "and time_created >= ? order by time_created desc limit 1",
        (cwd, int((_started_at(pid) - 5.0) * 1000)))
    return str(rows[0][0]) if rows else ""


# --- every conversation on disk, whichever agent wrote it -----------------------


@dataclass(frozen=True)
class Stored:
    """One conversation a harness has written down, found by its own store.

    `at` is when it was last written to (a file's mtime; for Hermes the last
    message, else when it started). `path` is the transcript, "" for Hermes,
    whose conversations live in a database.
    """
    session: str
    harness: str
    at: float
    path: str = ""
    #: The store's own name for the directory it ran in ("" when the store
    #: does not say, as Codex's does not).
    folder: str = ""


def _folder_of(harness: str, cwd: str) -> str:
    """What `cwd` is called in that harness's store, so a directory can be
    matched without opening a single conversation.

    Claude names a project directory after the path with every `/` and `.`
    turned into `-`; pi wraps the same shape in `--`. Codex files by date and
    says nothing about the directory, so it cannot be matched this way.
    """
    flat = re.sub(r"[/.]", "-", cwd.rstrip("/"))
    return f"--{flat.lstrip('-')}--" if harness == PI else flat


def _scan(root: Path, depth: int, name: re.Pattern, harness: str) -> list[Stored]:
    """Session files `depth` directories below `root`, by mtime.

    One `scandir` per directory and no file is opened: a list of a thousand
    conversations costs a stat each, which is what lets this be asked on
    every poll.
    """
    dirs = [root]
    for _ in range(depth):
        below = []
        for d in dirs:
            try:
                below += [Path(e.path) for e in os.scandir(d) if e.is_dir()]
            except OSError:
                continue
        dirs = below
    out = []
    for d in dirs:
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            m = name.fullmatch(e.name)
            if not m or not e.is_file():
                continue
            try:
                at = e.stat().st_mtime
            except OSError:
                continue
            out.append(Stored(m.group(1), harness, at, e.path, d.name))
    return out


def _hermes_stored() -> list[Stored]:
    """Every Hermes conversation, in every profile's store.

    Unlike `_hermes_rows` this asks all of them: a listing is the union of
    the profiles, not whichever one answers first.
    """
    import sqlite3

    out = []
    for db in hermes_stores():
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0) as c:
                rows = c.execute(
                    "select s.id, coalesce(max(m.timestamp), s.ended_at, s.started_at, 0) "
                    "from sessions s left join messages m on m.session_id = s.id "
                    "group by s.id").fetchall()
        except sqlite3.Error:
            continue
        for sid, at in rows:
            if _safe(str(sid or "")):
                out.append(Stored(str(sid), HERMES, float(at or 0.0)))
    return out


def _opencode_stored() -> list[Stored]:
    """Every opencode conversation. A subagent's session (it has a parent) is
    part of its parent's, not one of its own; an archived one was put away."""
    return [Stored(str(sid), OPENCODE, float(at or 0) / 1000.0)
            for sid, at in opencode_rows(
                "select id, time_updated from session "
                "where parent_id is null and time_archived is null")
            if is_opencode(str(sid or ""))]


_CODEX_META: dict[str, dict] = {}


def _codex_meta(path: str) -> dict:
    """A Codex rollout's first record (`session_meta`), or {}. A rollout never
    changes its first line, so each file is read once."""
    got = _CODEX_META.get(path)
    if got is None:
        try:
            with open(path, encoding="utf-8") as fh:
                rec = json.loads(fh.readline() or "{}")
        except (OSError, ValueError):
            return {}
        got = _CODEX_META[path] = (rec.get("payload") or {}) if rec.get("type") == "session_meta" else {}
    return got


def codex_subagent(path: str) -> bool:
    """Whether a Codex rollout is a subagent's — the guardian that reviews an
    approval request, say — which is part of its parent's conversation, not
    one of its own. Its first record says so (`source.subagent`)."""
    source = _codex_meta(path).get("source")
    return isinstance(source, dict) and "subagent" in source


def _in_tmp(cwd: str) -> bool:
    import tempfile

    cwd = (cwd or "").rstrip("/")
    return any(cwd == t or cwd.startswith(t + "/")
               for t in {"/tmp", tempfile.gettempdir().rstrip("/")})


def codex_scripted(path: str) -> bool:
    """Whether a Codex rollout is a script's run, not a conversation: `codex
    exec` (originator `codex_exec`, source `exec`), or anything run from a
    temp directory. A script driving Codex is nobody talking — its answers
    were spoken aloud and shelved as threads (the Cloudflare DNS runs of
    25 Sep 2026), and David had not started any of them."""
    meta = _codex_meta(path)
    return (meta.get("originator") == "codex_exec" or meta.get("source") == "exec"
            or _in_tmp(str(meta.get("cwd") or "")))


def codex_run_scripted(session: str, cwd: str = "") -> bool:
    """For the hooks: whether this Codex session is a scripted run
    (`codex_scripted`), from the cwd the hook was given or its rollout."""
    if cwd and _in_tmp(cwd):
        return True
    found = transcript(session) if session else None
    return bool(found) and found[0] == CODEX and codex_scripted(str(found[1]))


def stored(*, since: float = 0.0, limit: int = 0,
           exclude: tuple[str, ...] = ()) -> list[Stored]:
    """Every conversation on this host, newest first, whichever agent held it.

    This is the whole of "what has been talked about here": the harnesses'
    own stores, not the sessions that happen to be running and not the ones
    that reached the library by speaking. `since` drops anything not written
    to since that epoch time, `limit` caps each harness (after the cut, so a
    quiet agent is never crowded out by a busy one), and `exclude` names
    directories to leave out, matched against each store's own name for them
    (`_folder_of`, so not a single file is opened). Codex files by date and
    says nothing about the directory, so `exclude` does not reach it.

    An id written twice — the same conversation under two project
    directories — is listed once, at its newest.
    """
    skip = {h: [_folder_of(h, d) for d in exclude if d.strip()] for h in (CLAUDE, PI)}
    found = _scan(_claude_dir() / "projects", 1, re.compile(f"({_UUID})\\.jsonl"), CLAUDE)
    found += [r for r in _scan(_codex_dir() / "sessions", 3,
                               re.compile(f"rollout-.*-({_UUID})\\.jsonl"), CODEX)
              if not codex_subagent(r.path) and not codex_scripted(r.path)]
    found += _scan(_pi_dir() / "sessions", 1, re.compile(f".*_({_UUID})\\.jsonl"), PI)
    found += _hermes_stored()
    found += _opencode_stored()
    from .deleted import deleted

    gone = deleted()
    newest: dict[str, Stored] = {}
    for row in found:
        if row.at < since or row.session in gone:
            continue
        # A directory the caller wants nothing from: a gateway's scratch
        # folder holds thousands of one-shot sessions nobody had.
        if any(row.folder == d or row.folder.startswith(d.rstrip("-") + "-")
               for d in skip.get(row.harness, ())):
            continue
        seen = newest.get(row.session)
        if seen is None or row.at > seen.at:
            newest[row.session] = row
    rows = sorted(newest.values(), key=lambda r: r.at, reverse=True)
    if limit > 0:
        kept, counts = [], dict.fromkeys(HARNESSES, 0)
        for row in rows:
            if counts.get(row.harness, 0) >= limit:
                continue
            counts[row.harness] = counts.get(row.harness, 0) + 1
            kept.append(row)
        rows = kept
    return rows


def registry_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "agent-media" / "agent-panes"


def register_pane(harness: str, session: str, pid: int, pane: str) -> bool:
    """Record that `pid` in `pane` is on `session` — for agents (pi) that
    leave no outside trace of it. Whether it was written."""
    if harness not in HARNESSES or not _safe(session) or not pane.startswith("%") or pid <= 0:
        return False
    d = registry_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{pane.lstrip('%')}.tmp"
        tmp.write_text(json.dumps({"harness": harness, "session": session,
                                   "pid": int(pid), "pane": pane}))
        tmp.replace(d / pane.lstrip("%"))
        return True
    except OSError:
        return False


def _registered() -> list[Running]:
    """Registry rows whose process is still that agent, still in that pane."""
    out = []
    for f in glob.glob(str(registry_dir() / "[0-9]*")):
        try:
            row = json.loads(Path(f).read_text())
            pid = str(int(row.get("pid") or 0))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        argv = _argv(pid)
        harness = str(row.get("harness") or "")
        # A pane id and a pid are both reused: believe the row only while the
        # process it names is the same agent, sitting in the same pane.
        if not argv or os.path.basename(argv[0]) != harness:
            continue
        pane = str(row.get("pane") or "")
        if _pane_of(pid) != pane:
            continue
        out.append(Running(int(pid), str(row.get("session") or ""), pane, harness))
    return out


def running() -> list[Running]:
    """Every live codex, pi, hermes and opencode, with the session each is on.

    Claude is not here: claude_sessions has Claude's own record of that, and
    the reply code keeps its extra fallbacks. A codex that has not been sent
    anything yet has no session and is left out.
    """
    out = [r for r in _registered() if r.harness == PI and r.session]
    for d in glob.glob("/proc/[0-9]*"):
        pid = os.path.basename(d)
        argv = _argv(pid)
        if not argv:
            continue
        if os.path.basename(argv[0]) == CODEX:
            sid = _codex_session(pid)
            if sid:
                out.append(Running(int(pid), sid, _pane_of(pid), CODEX))
        elif _hermes_pid(argv):
            sid = _hermes_session(pid)
            if sid:
                out.append(Running(int(pid), sid, _pane_of(pid), HERMES))
        elif os.path.basename(argv[0]) == OPENCODE and argv[1:2] not in (["serve"], ["web"]):
            sid = _opencode_live_session(pid, argv)
            if sid:
                out.append(Running(int(pid), sid, _pane_of(pid), OPENCODE))
    return out


# --- naming one ------------------------------------------------------------------


def set_name(session: str, title: str) -> bool:
    """Tell the agent itself what this conversation is called. Whether it took.

    A rename from the phone is kept by agent-media and shown on the shelf
    whatever happens here; this is the second half — the terminal calling it
    the same thing. Only two of the four can be told:

    * claude  `~/.claude/session-autoname/<id>`, the file its own `/rename`
              writes (handled by `conversation.set_session_name`, which owns
              that path).
    * hermes  `hermes sessions rename <id> <title>`, straight into its store.
    * codex   thread names live in its own `session_index.jsonl`, which it
              rewrites by appending; a half-written row there is codex's
              problem, not ours, so it is left alone.
    * pi      names come from `/name` typed into the session, and typing into
              a live agent to rename it would show up as a turn.
    """
    import subprocess

    title = " ".join((title or "").split())
    if not title or not _safe(session):
        return False
    if is_hermes(session):
        exe = shutil.which(HERMES) or str(_hermes_dir().parent / ".local/bin/hermes")
        try:
            done = subprocess.run([exe, "sessions", "rename", session, title],
                                  capture_output=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0
    return False


# --- starting one ----------------------------------------------------------------


def resume_argv(harness: str, session: str) -> list[str]:
    """The arguments (after the program) that reopen `session`."""
    if harness == CODEX:
        return ["resume", session]
    if harness == HERMES:
        return ["--tui", "--resume", session]
    if harness in (PI, OPENCODE):
        return ["--session", session]
    return ["--resume", session]


def fresh_argv(harness: str, session: str = "") -> list[str]:
    """The arguments for a new session; pi can be told its id up front.

    Hermes cannot: `--pass-session-id` only puts the id in its own prompt, so
    a Hermes started here is asked for its id afterwards, like codex. Nor can
    opencode, whose ids it makes itself.
    """
    if harness == PI and _safe(session):
        return ["--session-id", session]
    if harness == HERMES:
        return ["--tui"]
    return []


# --- having one at all -------------------------------------------------------------

def bin_path() -> str:
    """PATH with the places agents are installed, whatever the caller inherited.

    A canvas started by systemd has the user manager's PATH — no `~/.local/bin`,
    no bun, no npm — so `claude` was "not found" in a window that looked exactly
    like a working one. Everything that asks "is this agent here" or "where is
    it" asks through this, so the answer does not depend on who is asking.
    """
    home = Path.home()
    extra = [home / ".local" / "bin", home / ".bun" / "bin", home / ".npm-global" / "bin",
             home / ".claude" / "local", Path("/usr/local/bin"),
             # fnm puts the live node on PATH through a per-shell symlink dir
             # under /run; its `default` alias is the stable name for it.
             home / ".local" / "share" / "fnm" / "aliases" / "default" / "bin"]
    return os.pathsep.join([os.environ.get("PATH") or "", *map(str, extra)])


def program(name: str) -> str:
    """Absolute path of `name` (an agent, or `npm`), or "" when it is not here."""
    return shutil.which(name, path=bin_path()) or ""


def installed(harness: str) -> bool:
    return bool(program(harness))


@dataclass(frozen=True)
class Recipe:
    """How to get an agent, and how to sign into it.

    `install` and `update` are whole commands (the first word resolved through
    `program`, so `npm` is found the same way `claude` is); `login` and
    `status` are the arguments that follow the agent itself.

    Empty means "not offered from here", which is a real answer for two of
    them: pi has no interactive sign-in — it reads API keys from its settings
    and the environment — and Hermes is a git checkout its own installer makes,
    so it can be updated from here but not conjured.
    """
    install: tuple[str, ...] = ()
    update: tuple[str, ...] = ()
    login: tuple[str, ...] = ()
    logout: tuple[str, ...] = ()
    status: tuple[str, ...] = ()
    #: The npm package it is published as, for "is there a newer one?".
    package: str = ""
    #: …or the agent's own way of answering that, for one that is not a
    #: package: Hermes is a checkout, and only it knows about its remote.
    check: tuple[str, ...] = ()


#: One row per harness. The install channels are the ones these agents are
#: actually on here: claude and pi are npm globals, codex ships its own
#: updater, hermes is a checkout under `~/.hermes` that updates itself.
RECIPES: dict[str, Recipe] = {
    CLAUDE: Recipe(
        install=("npm", "install", "-g", "@anthropic-ai/claude-code"),
        update=("npm", "install", "-g", "@anthropic-ai/claude-code@latest"),
        login=("auth", "login"),
        logout=("auth", "logout"),
        status=("auth", "status"),
        package="@anthropic-ai/claude-code",
    ),
    CODEX: Recipe(
        install=("npm", "install", "-g", "@openai/codex"),
        update=("codex", "update"),
        # `--device-auth`: the browser flow's callback goes to localhost:1455,
        # which from the phone is the phone — nothing is listening there, and
        # the login server on this host binds 127.0.0.1 besides. Device auth
        # is a code read off the pane and typed into auth.openai.com, so it
        # works from whichever screen the person is holding.
        login=("login", "--device-auth"),
        logout=("logout",),
        status=("login", "status"),
        package="@openai/codex",
    ),
    PI: Recipe(
        install=("npm", "install", "-g", "@earendil-works/pi-coding-agent"),
        update=("pi", "update", "self"),
        package="@earendil-works/pi-coding-agent",
    ),
    HERMES: Recipe(
        update=("hermes", "update", "--yes"),
        login=("setup",),
        check=("update", "--check"),
    ),
    # An npm package that ships a native binary per platform, and upgrades
    # itself once here. `auth login` is a provider picker, driven in the
    # window like Hermes's `setup`. There is no logout here: `auth logout`
    # with no provider named is a picker too, and this runs it blind.
    OPENCODE: Recipe(
        install=("npm", "install", "-g", "opencode-ai"),
        update=("opencode", "upgrade"),
        login=("auth", "login"),
        status=("auth", "list"),
        package="opencode-ai",
    ),
}


def install_argv(harness: str) -> list[str]:
    """The command that gets `harness` here, or brings it up to date. [] if none.

    Which of the two it is depends on whether the agent is already installed:
    an agent that ships its own updater knows more about its installation than
    this table does, so once it is here it is asked rather than reinstalled.
    """
    r = RECIPES.get(harness)
    if not r:
        return []
    argv = list(r.update if (installed(harness) and r.update) else r.install)
    if not argv:
        return []
    return [program(argv[0]) or argv[0], *argv[1:]]


def login_argv(harness: str) -> list[str]:
    """The command that signs into `harness` interactively. [] when it has none."""
    r = RECIPES.get(harness)
    exe = program(harness)
    if not r or not r.login or not exe:
        return []
    return [exe, *r.login]


def logout_argv(harness: str) -> list[str]:
    """The command that signs `harness` out. [] when it has none.

    Unlike the sign-in, both of these are a deletion of stored credentials
    and nothing else: they ask nothing and exit at once, so the caller runs
    them for an answer rather than opening a window to watch.
    """
    r = RECIPES.get(harness)
    exe = program(harness)
    if not r or not r.logout or not exe:
        return []
    return [exe, *r.logout]


#: The version inside whatever `--version` prints: "codex-cli 0.156.0",
#: "2.1.278 (Claude Code)". Compared as strings of numbers, not as text.
_VERSION = re.compile(r"\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?")


def version_number(text: str) -> str:
    """The version in `text`, or "". Both ends of a comparison go through this."""
    found = _VERSION.search(text or "")
    return found.group(0) if found else ""


def _parts(version: str) -> tuple:
    """`1.2.10` as numbers, so it sorts above `1.2.9`. A pre-release suffix
    is dropped: this decides "is there a newer one", not which one to fetch."""
    head = version.split("-")[0].split("+")[0]
    return tuple(int(n) for n in head.split(".") if n.isdigit())


def is_behind(installed_version: str, latest: str) -> bool | None:
    """Is this one older than that one? None when either cannot be read."""
    a, b = version_number(installed_version), version_number(latest)
    if not a or not b:
        return None
    pa, pb = _parts(a), _parts(b)
    if not pa or not pb:
        return None
    return pa < pb


def latest_of(harness: str, timeout: float = 20.0) -> str:
    """The newest published version of `harness`, or "".

    npm is the only registry any of them are on, and Hermes is not on it —
    it is a checkout, and answering for it would mean a fetch. "" is the
    honest answer there, and the caller shows no update state at all rather
    than a guess.
    """
    import subprocess

    r = RECIPES.get(harness)
    npm = program("npm")
    if not r or not r.package or not npm:
        return ""
    try:
        done = subprocess.run([npm, "view", r.package, "version"],
                              capture_output=True, text=True, timeout=timeout,
                              check=False, env={**os.environ, "PATH": bin_path()})
    except (OSError, subprocess.SubprocessError):
        return ""
    return version_number(done.stdout or "") if done.returncode == 0 else ""


def update_check(harness: str, timeout: float = 90.0) -> tuple[bool | None, str]:
    """Ask the agent itself whether it is behind. `(behind, what it said)`.

    For the one that is not a package: `hermes update --check` fetches from
    its remotes and says either "Already up to date" or "Update available",
    which is the whole answer — there is no version to compare, and a commit
    id would mean nothing on the page. It touches the network (about ten
    seconds), so the caller caches it like the registry lookups.
    """
    import subprocess

    r = RECIPES.get(harness)
    exe = program(harness)
    if not r or not r.check or not exe:
        return None, ""
    try:
        done = subprocess.run([exe, *r.check], capture_output=True, text=True,
                              timeout=timeout, check=False,
                              env={**os.environ, "PATH": bin_path()})
    except (OSError, subprocess.SubprocessError):
        return None, ""
    said = (done.stdout or "") + (done.stderr or "")
    low = said.lower()
    # Both phrases are the agent's own; anything else is "could not say",
    # never "current", so a check that changes its wording cannot quietly
    # start claiming everything is up to date.
    if "update available" in low:
        behind = True
    elif "up to date" in low:
        behind = False
    else:
        return None, ""
    line = next((ln.strip() for ln in reversed(said.splitlines())
                 if "update available" in ln.lower() or "up to date" in ln.lower()), "")
    return behind, line[:120]


def auth_state(harness: str, timeout: float = 15.0) -> tuple[str, str]:
    """`(state, detail)` — "in", "out" or "unknown", and a line to show.

    Three of the five can be asked without opening a terminal: Claude
    answers `auth status` in JSON, Codex exits non-zero when it is signed out,
    and opencode counts its stored credentials. The other two are reported
    honestly as unknown rather than guessed at.
    """
    import subprocess

    r = RECIPES.get(harness)
    exe = program(harness)
    if not r or not r.status or not exe:
        return "unknown", ""
    try:
        done = subprocess.run([exe, *r.status], capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unknown", ""
    out = (done.stdout or "") + (done.stderr or "")
    if harness == CLAUDE:
        try:
            data = json.loads(done.stdout)
        except ValueError:
            return "unknown", " ".join(out.split())[:200]
        who = str(data.get("email") or data.get("authMethod") or "")
        return ("in" if data.get("loggedIn") else "out"), who
    if harness == OPENCODE:
        return _opencode_auth(out)
    line = " ".join(out.split())[:200]
    return ("in" if done.returncode == 0 and "logged in" in out.lower() else "out"), line


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _opencode_auth(said: str) -> tuple[str, str]:
    """What `opencode auth list` says, as a sign-in state.

    It draws a box: "N credentials" under the stored ones, then the providers
    whose API key is in the environment. Either is a way in. Neither is
    still not "out" — opencode's own free models need no sign-in at all, so
    a chat started with none works, and "out" would have `/ask` refuse it.
    """
    text = _ANSI.sub("", said)
    m = re.search(r"(\d+)\s+credentials?\b", text)
    stored = int(m.group(1)) if m else 0
    env = re.split(r"\bEnvironment\b", text, maxsplit=1)
    # "●  Venice AI VENICE_API_KEY": the provider, then the variable.
    from_env = re.findall(r"●\s+(.+?)\s+[A-Z][A-Z0-9_]+\s*$", env[1], re.M) \
        if len(env) > 1 else []
    if stored or from_env:
        said = [f"{stored} credential{'s' if stored != 1 else ''}"] if stored else []
        if from_env:
            said.append("keys for " + ", ".join(from_env[:3]))
        return "in", "; ".join(said)
    if not m:
        return "unknown", " ".join(text.split())[:200]
    return "unknown", "no sign-in: opencode's free models only"


def version_of(harness: str, timeout: float = 10.0) -> str:
    """What `harness --version` says, trimmed to one line. "" when it cannot say."""
    import subprocess

    exe = program(harness)
    if not exe:
        return ""
    try:
        done = subprocess.run([exe, "--version"], capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return " ".join((done.stdout or done.stderr or "").split())[:80]
