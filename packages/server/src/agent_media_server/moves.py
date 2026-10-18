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
   not. A session busy with something is not interrupted — a turn would
   lose whatever it had not written yet, a question its asker, a background
   agent or task its work — so its move waits (`waiting_on` says for what):
   it is kept in `<state_dir>/moves_pending.json` and carried out the
   moment the session is free (`_wait_then_move`, woken by sessiond's events
   for a headless session), and a server restart picks it up again
   (`start`). A later move replaces it; `cancel` drops it; one that fails
   when it runs is kept with why in `moves_failed.json`. David, 15 Oct 2026:
   move a thread mid-turn without having to end the session first; 19 Oct
   2026: background agents count as busy, and so does a question.

   A headless session (sessiond) has no pane to reopen: sessiond is told
   its new directory (`relocate`, which parks it if it sits idle), and its
   next message resumes it there.

4. **Its folder moves.** The conversation's own files are
   `<project>/<title>` under the Conversations root, so the folder is moved
   under the new project and the manifest points at it there: the files on
   disk and the app say the same thing, and `project_of` derives the new
   project from the folder even without (1).

   This costs the old Audiobookshelf app that conversation's listening
   progress — ABS reads a folder that moved as a *new* item, new id and all
   (the same reason `book_tracks.folder_for` keeps its first folder for
   ever). David decided on 23 Sep 2026 that agreement between the files and
   the app is worth more than progress in an app on its way out.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

from . import auth, panes, sessions
from ._jsonmap import JsonMap

MOVES = JsonMap("moved.json")
# Moves asked for while the session was busy, {session: {project, cwd, at,
# why_waiting}}, until it is free (`waiting_on`).
PENDING = JsonMap("moves_pending.json")
# Waiting moves that could not be carried out, {session: {project, cwd, at,
# failed_at, error}}, until the next move or a cancel.
FAILED = JsonMap("moves_failed.json")
# How often a waiting move looks at a pane session; how long it waits on a
# headless one's events before it looks anyway; and how long it waits for a
# session to be free before it gives up (busy that long is stuck).
POLL_S = 3.0
BACKSTOP_S = 15.0
WAIT_MAX_S = 6 * 3600.0
_WAITING: set[str] = set()
_WAITING_LOCK = threading.Lock()


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
    hits = sorted(root.glob(f"*/{session}.jsonl"),
                  key=lambda f: f.stat().st_mtime, reverse=True)
    if not hits:
        return ""
    # The newest is the live one. A resume that ran in the old directory
    # after an earlier move (sessiond resumed headless sessions where they
    # had started, 18 Oct 2026) left a stale copy behind it, and the first
    # by name was moved instead of the one still being written.
    src = hits[0]
    _set_aside(session, hits[1:])
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


def _set_aside(session: str, stale: list[Path]) -> None:
    """Move older copies of a transcript out of `~/.claude/projects`, into
    `~/.cache/claude-stale-transcripts/<session>-<when>/<encoded dir>.jsonl`,
    so nothing reads or resumes them as the session. Best effort."""
    if not stale:
        return
    aside = (Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
             / "claude-stale-transcripts"
             / f"{session[:8]}-{time.strftime('%Y%m%d-%H%M%S')}")
    for f in stale:
        try:
            aside.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(aside / f"{f.parent.name}.jsonl"))
        except OSError:
            pass


# --- where the conversation's own files live ----------------------------------

def move_folder(session: str, project: str) -> tuple[str, str]:
    """Move the conversation's library folder under `project`.

    `(new folder, error)` — `("", "")` when there is nothing to move.

    `<Conversations root>/<project>/<title>`: the title part is kept exactly
    as it is (`book_tracks.folder_for` chose it once and an item that renames
    itself is a new item), only the project above it changes. The manifest is
    rewritten to point at the new path, which is what makes
    `sessions.project_of` agree without the override.

    Nothing published yet, no manifest, or a folder already gone: "" — the
    move is about a thread, and a thread with no files still moves.
    """
    from agent_media_core import book_tracks

    if not project:
        return "", ""                   # a directory outside any project
    path = sessions._manifest_dir() / f"{session}.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return "", ""
    raw = str(data.get("folder") or "").strip()
    if not raw:
        return "", ""                   # Path("") is ".", the server's own cwd
    folder = Path(raw)
    if not folder.is_absolute() or not folder.is_dir():
        return "", ""
    dest = book_tracks.root() / book_tracks.safe_name(project) / folder.name
    if dest == folder:
        return "", ""
    if dest.exists():
        return "", f"{dest} is already there"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder), str(dest))
    except OSError as e:
        return "", f"could not move the folder: {e}"
    data["folder"] = str(dest)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except OSError as e:
        # The files moved and the manifest still points at where they were:
        # say so rather than leave the caller thinking it went cleanly.
        return str(dest), f"moved the folder, but could not record it: {e}"
    return str(dest), ""


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


def _agents_running(session: str) -> bool:
    """Whether a background agent of the session is still at work (§6.12),
    read off its transcript's subagent files. A move closes or parks the
    process, and an agent running in it would die with it."""
    from . import agents

    try:
        return agents.counts(agents.agents(session, live=True))["running"] > 0
    except Exception:  # noqa: BLE001 — unreadable is not running
        return False


def waiting_on(session: str, pane: str = "") -> str:
    """What a move of that session has to wait for, or "" when it may go now.

    - `"turn"`: a turn is running (or starting, or a message is queued
      behind one) — interrupted, it loses whatever it had not written yet.
    - `"question"`: it is stopped on a permission or a question somebody
      has to answer; parked, that request is lost.
    - `"agents"`: its turn is over but background work is not — a subagent,
      or for a headless session any task sessiond still hears from (a
      background Bash too). Parking or closing the process kills them.

    `sessions.activity_of` answers for the turn with a pane or without one;
    sessiond (`headless_state`) answers for its own sessions, including the
    tasks it counts. A pane session's background Bash is not knowable from
    here, so for one only its subagents hold a move. A session that is not
    running has nothing to lose.
    """
    from . import driver

    if not pane:
        hl = driver.headless_state(session)
        if hl is None or not hl.get("live"):
            return ""
        raw = str(hl.get("raw") or hl.get("state") or "")
        if raw in ("working", "starting") or hl.get("state") == "working" \
                or hl.get("queued"):
            return "turn"
        if hl.get("approval") or raw == "approval" or hl.get("state") == "approval":
            return "question"
        if int(hl.get("tasks") or 0) > 0 or _agents_running(session):
            return "agents"
        return ""
    st = sessions.activity_of(session, pane)["state"]
    if st == "working":
        return "turn"
    if st == "approval":
        return "question"
    return "agents" if _agents_running(session) else ""


def _busy(session: str, pane: str) -> bool:
    """Whether that session must not be moved now (`waiting_on`)."""
    return bool(waiting_on(session, pane))


def _pending_answer(session: str, row: dict, pane: str) -> dict:
    return {"session": session, "project": row.get("project") or None, "cwd": row["cwd"],
            "pending": True, "why_waiting": row.get("why_waiting") or "turn",
            "move_pending": pending_view(session), "restarted": False,
            "pane": pane or None, "live": True, "folder": None}


def move(session: str, project: str = "", cwd: str = "",
         bearer: str = "") -> tuple[bool, dict]:
    """Move a conversation to another project. `(ok, detail)`.

    `detail` carries `project`, `cwd`, whether the session was restarted and
    the pane it came back in, so the app can offer a link to it. A session
    that cannot be moved now (`waiting_on`) is answered `pending: true` with
    `why_waiting`, and moved by a waiter the moment it can be.
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

    # A new ask is a new start: an earlier one's failure is not news now.
    FAILED.drop(session)
    pane = sessions.live_sessions().get(session, "")
    waiting = waiting_on(session, pane)
    if not waiting:
        # A move asked for now replaces one still waiting.
        PENDING.drop(session)
        ok, detail = _carry_out(session, project, dest)
        waiting = str(detail.pop("busy", "") or "")
        if not waiting:
            return ok, detail
    # Pane or no pane: a running turn, a question or background work is left
    # to finish; the move is kept and carried out once it has. A second
    # move replaces the first.
    now = round(time.time(), 3)
    row = {"project": project, "cwd": dest, "at": now, "why_waiting": waiting}
    PENDING.put(session, row)
    _watch(session)
    return True, _pending_answer(session, row, pane)


def cancel(session: str, bearer: str = "") -> tuple[bool, dict]:
    """Drop the move waiting on that session, and any failure it left.
    `{"session", "cancelled": bool}` — false when nothing was waiting."""
    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    had = PENDING.drop(session)
    FAILED.drop(session)
    return True, {"session": session, "cancelled": bool(had)}


def _carry_out(session: str, project: str, dest: str) -> tuple[bool, dict]:
    """The move itself, on a session that is free to move: close a live one,
    file it, move its transcript and folder, and reopen it in `dest`.

    `detail["busy"]` (with ok False) when the session turned out to be busy
    after all — sessiond refused to park it because a turn started between
    the look and the move — so the caller keeps the move waiting."""
    from . import send

    pane = sessions.live_sessions().get(session, "")
    live = bool(pane and panes.alive(pane))
    if live:
        ok, detail = send.close_pane(session, pane)
        if not ok:
            return False, detail

    # A headless session resumes wherever sessiond has it, whatever the
    # transcript says: tell sessiond first (it parks one sitting idle), or
    # its next message brings it back in the old directory.
    from . import driver

    if driver.owned_headless(session):
        why = driver.headless_driver().relocate(session, dest)
        if why:
            busy = waiting_on(session, "")
            if busy:
                return False, {"busy": busy, "error": why}
            return False, {"error": why, "project": project, "cwd": dest,
                           "restarted": False, "status": 503}

    why = move_transcript(session, dest)
    if why:
        # The transcript stayed put, so a resume would still find it where it
        # was: file the thread, leave it closed, and say so.
        MOVES.put(session, {"project": project, "cwd": dest, "at": round(time.time(), 3)})
        return False, {"error": why, "project": project, "cwd": dest,
                       "restarted": False, "status": 500}

    MOVES.put(session, {"project": project, "cwd": dest, "at": round(time.time(), 3)})

    out = {"session": session, "project": project or None, "cwd": dest,
           "restarted": False, "pane": None, "live": False, "folder": None}
    # The conversation's own files follow. Not fatal on its own — the thread
    # has moved either way — so a failure is reported beside the move, not
    # instead of it.
    folder, why = move_folder(session, project)
    out["folder"] = folder or None
    if why:
        out["folder_error"] = why
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


def pending(session: str) -> dict:
    """The move waiting on that session, `{project, cwd, at, why_waiting}`, or {}."""
    row = PENDING.get(session)
    return row if isinstance(row, dict) else {}


def failed(session: str) -> dict:
    """The last waiting move that could not be carried out, `{project, cwd,
    at, failed_at, error}`, or {} — until the next move or a cancel."""
    row = FAILED.get(session)
    return row if isinstance(row, dict) else {}


def pending_view(session: str) -> dict | None:
    """`move_pending` as a thread shows it (§6.15): `{project, cwd, since,
    why_waiting}`, or None."""
    row = pending(session)
    if not row:
        return None
    return {"project": row.get("project") or None, "cwd": row.get("cwd") or "",
            "since": row.get("at"), "why_waiting": row.get("why_waiting") or "turn"}


def failed_view(session: str) -> dict | None:
    """`move_failed` as a thread shows it: `{project, cwd, at, error}`, or None."""
    row = failed(session)
    if not row:
        return None
    return {"project": row.get("project") or None, "cwd": row.get("cwd") or "",
            "at": row.get("failed_at") or row.get("at"), "error": row.get("error") or ""}


def add_status(row: dict, session: str) -> None:
    """`move_pending` / `move_failed` on a thread row or envelope, in place,
    each only when there is one (old apps never see a new key they would
    have to ignore on a thread that has none)."""
    p = pending_view(session)
    if p:
        row["move_pending"] = p
    else:
        row.pop("move_pending", None)
    f = failed_view(session)
    if f:
        row["move_failed"] = f
    else:
        row.pop("move_failed", None)


def _fail(session: str, row: dict, error: str) -> None:
    FAILED.put(session, {"project": row.get("project") or "", "cwd": row.get("cwd") or "",
                         "at": row.get("at"), "failed_at": round(time.time(), 3),
                         "error": error})
    print(f"move: {session[:8]} to {row.get('project') or row.get('cwd')} failed: {error}",
          file=sys.stderr, flush=True)


def _same(a: dict, b) -> bool:
    """The same asked-for move, whatever `why_waiting` says by now."""
    return isinstance(b, dict) and all(a.get(k) == b.get(k) for k in ("project", "cwd", "at"))


def _take(session: str, row: dict) -> bool:
    """Drop `row` from the waiting moves if it is still the one there."""
    return PENDING.update(lambda rows: _same(row, rows.get(session))
                          and rows.pop(session, None) is not None)


def _watch(session: str) -> None:
    """One waiter per session, in the background."""
    with _WAITING_LOCK:
        if session in _WAITING:
            return
        _WAITING.add(session)
    threading.Thread(target=_wait_then_move, args=(session,),
                     name=f"move-{session[:8]}", daemon=True).start()


def _events_seq(session: str) -> int | None:
    """sessiond's event count for a headless session, None for the rest
    (or with sessiond down)."""
    from . import driver

    if not driver.owned_headless(session):
        return None
    from .driver.headless import call

    r = call("events", session=session, since=1 << 62, timeout=3.0)
    return int(r["seq"]) if r.get("ok") else None


def _pause(session: str, seq: int | None) -> None:
    """Until something may have changed. A headless session is watched on
    sessiond's own events (a turn ending, a task finishing, a question
    answered all are one), so the move goes the moment the session is free;
    `BACKSTOP_S` bounds the wait, for what changes without an event (a task
    gone stale). A pane session is looked at every `POLL_S`."""
    if seq is not None:
        from .driver.headless import call

        t0 = time.monotonic()
        r = call("events", session=session, since=seq, wait=BACKSTOP_S,
                 timeout=BACKSTOP_S + 5.0)
        if r.get("ok") or time.monotonic() - t0 >= POLL_S:
            return
    time.sleep(POLL_S)


def _wait_then_move(session: str) -> None:
    """Wait until the session is free (`waiting_on`), then carry out the
    move still asked for.

    The row is read again on every look, so a later move replaces the
    destination, a cancel or a move carried out meanwhile (each drops the
    row) ends the wait, and a message sent meanwhile only means more
    waiting. Auth was checked when the move was asked for. A move that
    cannot be carried out — its directory gone, a step refused, or a
    session still busy after `WAIT_MAX_S` — is kept in `moves_failed.json`
    with why, and the thread shows it (`move_failed`).
    """
    try:
        while True:
            row = pending(session)
            if not row:
                return
            if time.time() - float(row.get("at") or 0) > WAIT_MAX_S:
                if PENDING.update(lambda rows: _same(row, rows.get(session))
                                  and rows.pop(session, None) is not None):
                    _fail(session, row, f"still busy after {WAIT_MAX_S / 3600:.0f} hours "
                          f"({row.get('why_waiting') or 'turn'}); the move was dropped")
                return
            seq = _events_seq(session)
            pane = sessions.live_sessions().get(session, "")
            why = waiting_on(session, pane)
            if why:
                if why != row.get("why_waiting"):
                    PENDING.update(lambda rows: _same(row, rows.get(session))
                                   and rows.__setitem__(session, {**row, "why_waiting": why})
                                   is None)
                _pause(session, seq)
                continue
            # Carry out only the row read here: one asked for since (or a
            # cancel) is read again. It stays recorded until the move is over.
            if not _same(row, pending(session)):
                continue
            dest = str(row.get("cwd") or "")
            if not os.path.isdir(dest):
                if _take(session, row):
                    _fail(session, row, f"{dest} is no longer a directory on this host")
                return
            ok, detail = _carry_out(session, str(row.get("project") or ""), dest)
            busy = str(detail.pop("busy", "") or "")
            if busy:
                # Busy again between the look and the move: back to waiting.
                PENDING.update(lambda rows: _same(row, rows.get(session))
                               and rows.__setitem__(session, {**row, "why_waiting": busy})
                               is None)
                continue
            replaced = not _take(session, row)
            if replaced:
                continue                # another move came in: that one next
            if ok:
                print(f"move: {session[:8]} to {row.get('project') or row.get('cwd')} "
                      f"once it was free: done", file=sys.stderr, flush=True)
            else:
                _fail(session, row, str(detail.get("error") or "the move failed"))
            return
    except Exception as e:  # a waiter must not take the server down
        print(f"move: waiting on {session[:8]} failed: {e}", file=sys.stderr, flush=True)
    finally:
        with _WAITING_LOCK:
            _WAITING.discard(session)


def start() -> None:
    """Pick up the moves a restart left waiting."""
    for session in PENDING.all():
        _watch(session)
