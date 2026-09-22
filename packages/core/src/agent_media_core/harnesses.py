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

CLAUDE, CODEX, PI, HERMES = "claude", "codex", "pi", "hermes"
HARNESSES = (CLAUDE, CODEX, PI, HERMES)

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
#: Hermes numbers its sessions by the clock instead: `20260921_102508_f74b02`.
_HERMES_ID = r"[0-9]{8}_[0-9]{6}_[0-9a-f]{4,}"
#: Either shape, for anything that takes a session id from outside.
SESSION_ID = re.compile(f"(?:{_UUID}|{_HERMES_ID})")
_ROLLOUT = re.compile(r"rollout-.*-(" + _UUID + r")\.jsonl$")


def _claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def _codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def _pi_dir() -> Path:
    return Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent").expanduser()


def _hermes_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


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
    """"claude", "codex", "pi", "hermes", or "" when none of them has it."""
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
    """The first thing the person asked, for codex, pi and hermes. "" otherwise."""
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
    are read by session_feed, which knows its record types.
    """
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
    found += _scan(_codex_dir() / "sessions", 3, re.compile(f"rollout-.*-({_UUID})\\.jsonl"), CODEX)
    found += _scan(_pi_dir() / "sessions", 1, re.compile(f".*_({_UUID})\\.jsonl"), PI)
    found += _hermes_stored()
    newest: dict[str, Stored] = {}
    for row in found:
        if row.at < since:
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
    """Every live codex, pi and hermes, with the session each is on.

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
    if harness == PI:
        return ["--session", session]
    return ["--resume", session]


def fresh_argv(harness: str, session: str = "") -> list[str]:
    """The arguments for a new session; pi can be told its id up front.

    Hermes cannot: `--pass-session-id` only puts the id in its own prompt, so
    a Hermes started here is asked for its id afterwards, like codex.
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
    status: tuple[str, ...] = ()


#: One row per harness. The install channels are the ones these agents are
#: actually on here: claude and pi are npm globals, codex ships its own
#: updater, hermes is a checkout under `~/.hermes` that updates itself.
RECIPES: dict[str, Recipe] = {
    CLAUDE: Recipe(
        install=("npm", "install", "-g", "@anthropic-ai/claude-code"),
        update=("npm", "install", "-g", "@anthropic-ai/claude-code@latest"),
        login=("auth", "login"),
        status=("auth", "status"),
    ),
    CODEX: Recipe(
        install=("npm", "install", "-g", "@openai/codex"),
        update=("codex", "update"),
        login=("login",),
        status=("login", "status"),
    ),
    PI: Recipe(
        install=("npm", "install", "-g", "@earendil-works/pi-coding-agent"),
        update=("pi", "update", "self"),
    ),
    HERMES: Recipe(
        update=("hermes", "update", "--yes"),
        login=("setup",),
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


def auth_state(harness: str, timeout: float = 15.0) -> tuple[str, str]:
    """`(state, detail)` — "in", "out" or "unknown", and a line to show.

    Only two of the four can be asked without opening a terminal: Claude
    answers `auth status` in JSON, Codex exits non-zero when it is signed out.
    The other two are reported honestly as unknown rather than guessed at.
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
    line = " ".join(out.split())[:200]
    return ("in" if done.returncode == 0 and "logged in" in out.lower() else "out"), line


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
