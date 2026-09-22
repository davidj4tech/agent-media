"""Moving a conversation to another project.

A thread has no project of its own to set. `sessions.project_of` reads one
off two things this server did not choose: the directory the session ran in,
and the shelf folder the conversation is filed under. So "move this to
agent-mail" is not a field to write — it is a question of which of those to
change, and what a live session does about it.

What a move does, in order:

1. **Files it here.** `<state_dir>/moved.json`, `{"<session>": {"project",
   "cwd", "at"}}`. `sessions.session_cwd` and `sessions.add_projects` ask
   this first, so the thread lists, groups and searches under its new
   project at once, whatever its transcript says.
2. **Moves the transcript.** `claude --resume <id>` only finds a session
   from the directory it ran in — transcripts are filed under
   `~/.claude/projects/<encoded cwd>/` — so a session that is to come back
   in a new directory has to be filed under that one. Claude Code only;
   Codex, pi and Hermes keep their own stores and are moved by (1) alone.
3. **Restarts a live session there.** The pane is closed and reopened with
   `--resume` in the new directory, in the tmux session the new project
   uses. Claude Code cannot change its own cwd mid-session, so this is the
   only honest way to do it: the conversation continues, the process does
   not. A session working on something is refused — an interrupted turn
   would lose whatever it had not written yet.

What a move deliberately does **not** touch is the library. The shelf folder
is `<author>/<title>` under the Conversations root, and Audiobookshelf reads
a folder that moves as a *new item*: new id, no progress, the old one left
behind (the same reason `book_tracks.folder_for` keeps the first folder for
ever). One item that keeps its identity is worth more than one filed under
the right author, so the conversation stays where it was published and the
project it shows in the app is the one kept here.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

from . import auth, panes, sessions
from ._jsonmap import JsonMap

MOVES = JsonMap("moved.json")


def claude_root() -> Path:
    """`~/.claude/projects`, or wherever `CLAUDE_CONFIG_DIR` puts it."""
    from agent_media_core import harnesses

    return harnesses._claude_dir() / "projects"


def moved(session: str) -> dict:
    """`{"project", "cwd", "at"}` for a moved session, `{}` for the rest."""
    row = MOVES.get(session)
    return row if isinstance(row, dict) else {}


def moved_cwd(session: str) -> str:
    return str(moved(session).get("cwd") or "")


def moved_project(session: str) -> str:
    return str(moved(session).get("project") or "")


def forget(session: str) -> bool:
    """Drop the override, so the thread goes back to deriving its project."""
    return MOVES.drop(session)


# --- where a transcript lives -------------------------------------------------

def encoded_dir(cwd: str) -> str:
    """The `~/.claude/projects` directory name for `cwd`.

    Claude Code replaces every character that is not a letter or a digit with
    a dash: `/home/ryer/projects/agent-media` →
    `-home-ryer-projects-agent-media`, `/home/ryer/.meridian` →
    `-home-ryer--meridian` (the dot is a dash of its own, which is why the
    two run together).
    """
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def transcript_dir(cwd: str) -> Path:
    """Where Claude Code files sessions run in `cwd`.

    An existing directory that already holds a session recorded in `cwd`
    wins over the encoding rule: the rule is read off the names this host
    has, and a Claude Code that ever changes it would otherwise strand the
    moved thread somewhere `--resume` does not look.
    """
    root = claude_root()
    guess = root / encoded_dir(cwd)
    if guess.is_dir():
        return guess
    for d in sorted(root.glob("*")):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.jsonl"))[:1]:
            if sessions.transcript_cwd(f.stem) == cwd:
                return d
    return guess


def move_transcript(session: str, cwd: str) -> str:
    """File `session`'s Claude transcript under `cwd`. "" or why not.

    The sidecar directory beside it (`<session>/`, where subagent transcripts
    go) travels with it. Nothing to move — another harness, or a session
    whose file has already gone — is not an error: the override alone still
    moves the thread.
    """
    root = claude_root()
    hits = sorted(root.glob(f"*/{session}.jsonl"))
    if not hits:
        return ""
    src = hits[0]
    dest = transcript_dir(cwd)
    if dest == src.parent:
        return ""
    try:
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest / src.name))
    except OSError as e:
        return f"could not move the transcript: {e}"
    side = src.parent / session
    if side.is_dir():
        try:
            shutil.move(str(side), str(dest / session))
        except OSError:
            pass                        # the thread moved; its subagents did not
    sessions._CWDS.pop(session, None)
    return ""


# --- the move -----------------------------------------------------------------

def _destination(project: str, cwd: str) -> tuple[str, str, str]:
    """`(project, cwd, error)` for what the caller asked for.

    Either names it: a directory (`cwd`), whose project is whatever the
    layout calls it, or a project, whose directory is the one its newest
    conversation ran in (`sessions.project_target`).
    """
    cwd = os.path.expanduser((cwd or "").strip())
    project = (project or "").strip()
    if cwd:
        if not os.path.isdir(cwd):
            return "", "", f"{cwd} is not a directory on this host"
        return project or (sessions.project_of(cwd) or ""), cwd, ""
    if not project:
        return "", "", "no project or directory given"
    _host, found = sessions.project_target(project)
    if not found:
        return "", "", f"no directory known for project {project!r}"
    return project, found, ""


def _busy(session: str, pane: str) -> bool:
    """Whether that session is mid-turn, and so must not be restarted.

    Read off the pane the same way `send` reads it, because a headless
    session has no pane to read: `driver` answers for that one.
    """
    if not pane:
        return False
    cap = panes.strip_ansi(sessions._capture_pane(pane))
    return panes.classify(cap, sessions.agent_of(session)) == "working"


def move(session: str, project: str = "", cwd: str = "",
         bearer: str = "") -> tuple[bool, dict]:
    """Move a conversation to another project. `(ok, detail)`.

    `detail` carries `project`, `cwd`, whether the session was restarted and
    the pane it came back in, so the app can offer a link to it.
    """
    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    project, dest, why = _destination(project, cwd)
    if why:
        return False, {"error": why, "status": 400}
    if not sessions.session_exists(session):
        return False, {"error": f"session {session[:8]} has no transcript", "status": 404}

    from . import send

    pane = sessions.live_sessions().get(session, "")
    live = bool(pane and panes.alive(pane))
    if live and _busy(session, pane):
        return False, {"error": "that session is working — stop it first",
                       "pane": pane, "status": 409}
    if live:
        ok, detail = send.close_pane(session, pane)
        if not ok:
            return False, detail

    why = move_transcript(session, dest)
    if why:
        # The transcript stayed put, so a resume would still find it where it
        # was: file the thread, leave it closed, and say so.
        MOVES.put(session, {"project": project, "cwd": dest, "at": round(time.time(), 3)})
        return False, {"error": why, "project": project, "cwd": dest,
                       "restarted": False, "status": 500}

    MOVES.put(session, {"project": project, "cwd": dest, "at": round(time.time(), 3)})

    out = {"session": session, "project": project or None, "cwd": dest,
           "restarted": False, "pane": None, "live": False}
    if not live:
        return True, out

    from agent_media_core import layout

    host = layout.project_host(project) if project and layout.projects() else ""
    pane, why = send.open_window(session, dest, resume=True, host=host,
                                 agent=sessions.agent_of(session))
    if why:
        # It is moved either way; it just did not come back up. Say both.
        out["error"] = why
        out["pane"] = pane or None
        return False, out
    out.update(restarted=True, pane=pane, live=True)
    return True, out
