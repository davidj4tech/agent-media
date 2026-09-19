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
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

CLAUDE, CODEX, PI = "claude", "codex", "pi"
HARNESSES = (CLAUDE, CODEX, PI)

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ROLLOUT = re.compile(r"rollout-.*-(" + _UUID + r")\.jsonl$")


def _claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def _codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def _pi_dir() -> Path:
    return Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent").expanduser()


def _safe(session: str) -> bool:
    return bool(re.fullmatch(_UUID, session or ""))


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
    """"claude", "codex", "pi", or "" when no file of any of them has it."""
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
    """The first thing the person asked, for codex and pi. "" otherwise."""
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
    """Every live codex and pi, with the session each is on.

    Claude is not here: claude_sessions has Claude's own record of that, and
    the reply code keeps its extra fallbacks. A codex that has not been sent
    anything yet has no session and is left out.
    """
    out = [r for r in _registered() if r.harness == PI and r.session]
    for d in glob.glob("/proc/[0-9]*"):
        pid = os.path.basename(d)
        argv = _argv(pid)
        if not argv or os.path.basename(argv[0]) != CODEX:
            continue
        sid = _codex_session(pid)
        if sid:
            out.append(Running(int(pid), sid, _pane_of(pid), CODEX))
    return out


# --- starting one ----------------------------------------------------------------


def resume_argv(harness: str, session: str) -> list[str]:
    """The arguments (after the program) that reopen `session`."""
    if harness == CODEX:
        return ["resume", session]
    if harness == PI:
        return ["--session", session]
    return ["--resume", session]


def fresh_argv(harness: str, session: str = "") -> list[str]:
    """The arguments for a new session; pi can be told its id up front."""
    if harness == PI and _safe(session):
        return ["--session-id", session]
    return []
