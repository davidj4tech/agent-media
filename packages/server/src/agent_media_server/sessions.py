"""Which sessions there are, where they run, and what each is doing.

Moved out of the canvas's reply.py. Everything here reads and nothing types:
the live-process walk (`live_sessions`), the shelf of conversations the
library was made from (`sessions_index`, `places`), what a pane is showing
(the ghost prompt, a dialog it is stopped on) and the working / waiting /
approval states the app's shelf filters on. The routes that answer from it
are `/targets`, `/conversations` and `/sessions/state`; sending lives in
`send`.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from . import auth, auth_abs, panes, procmem, recaps


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: What counts as a session id from outside — a uuid (claude, codex, pi) or
#: Hermes's clock-stamped `20260921_102508_f74b02`. The gates below take this;
#: `_UUID` stays where the shape itself is the point (claude's argv).
_SESSION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                      r"|[0-9]{8}_[0-9]{6}_[0-9a-f]{4,}")


# --- which conversation, and so which session ---------------------------------


def _manifest_dir() -> Path:
    from agent_media_core._paths import state_dir

    return state_dir() / "book-tracks"


def _tail(path: str) -> str:
    """The `<author>/<title>` tail two ABS and this host agree on.

    ABS reports the path inside its own mount; we know the host's, and nothing
    tells either how the other maps. `_find_item` in book_tracks matches on the
    same tail going the other way.
    """
    parts = [p for p in str(path).replace("\\", "/").split("/") if p]
    return "/".join(parts[-2:])


def session_for_item(item_id: str, bearer: str) -> tuple[str | None, str]:
    """The session uuid behind an ABS library item, or (None, why not).

    Looked up with the *caller's* bearer, so an item they cannot see is an item
    they cannot reply to, decided by ABS.
    """
    item_id = (item_id or "").strip()
    if not item_id:
        return None, "no item id"
    # A device token is never shown to ABS; a device looks items up under the
    # host's own ABS login (see auth.abs_bearer).
    bearer = auth.abs_bearer(bearer)
    url = auth_abs.abs_home(bearer)
    if not url:
        return None, "no Audiobookshelf configured on this host"
    item, status = auth_abs._abs_get(url, bearer, f"/api/items/{item_id}")
    if not item:
        # The status is the difference between "that item is not there" and
        # "Audiobookshelf did not answer", which were both reported as the
        # first one — so an outage sent you looking for a missing item, and a
        # reply that failed because the server blinked said the library was
        # wrong. Only a real 404 is a missing item.
        if status == 404:
            return None, "no such item"
        if not status:
            return None, f"Audiobookshelf ({url}) did not answer"
        return None, f"Audiobookshelf ({url}) said {status} for that item"
    return session_for_path(item.get("path") or "")


def session_for_path(path: str) -> tuple[str | None, str]:
    """The session uuid behind an item's folder, or (None, why not).

    The half of `session_for_item` that needs no server: a caller already
    holding the item (the `/item` route does) can ask this directly.
    """
    tail = _tail(path or "")
    if not tail:
        return None, "item has no path"
    for f in sorted(_manifest_dir().glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if _tail(data.get("folder") or "") == tail:
            return str(data.get("session") or f.stem), ""
    return None, "not a conversation (no session behind it)"


# --- is that session live, and where ------------------------------------------


#: Folders whose agents are machinery, not conversations: Meridian (the
#: gateway) keeps a pool of Claude Code sessions in `~/.meridian`, each in a
#: `_meridian` tmux pane, and serves requests through them. They are not
#: David's threads — they must not appear in the thread list, and the idle
#: reaper must never close them (it would take the gateway down with it; the
#: first dry run on 2026-09-22 listed three as would-close). Comma-separated,
#: `~` expanded; MEDIA_SESSIONS_EXCLUDE_CWD overrides the default.
_EXCLUDE_DEFAULT = "~/.meridian"


def _excluded_dirs() -> list[str]:
    raw = os.environ.get("MEDIA_SESSIONS_EXCLUDE_CWD")
    raw = _EXCLUDE_DEFAULT if raw is None else raw
    return [os.path.realpath(os.path.expanduser(p.strip())) for p in raw.split(",") if p.strip()]


def _is_machinery(pid: int, excluded: list[str]) -> bool:
    """Is this agent process running in an excluded folder?"""
    if not excluded:
        return False
    try:
        cwd = os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return False
    return any(cwd == d or cwd.startswith(d + os.sep) for d in excluded)


def live_sessions() -> dict[str, str]:
    """`{session uuid: pane}` for every Claude Code process in a pane.

    A pane is tmux's or herdr's, addressed as `panes` writes it — both hand
    their pane id down to the agent they start, so both are found the same way.

    Claude Code's `~/.claude/sessions/<pid>.json` names the session; failing
    that, the same detection as `claude-resume`: a session's uuid is in argv
    when it was resumed, and only in the SessionStart registry when it was
    started fresh. Driven off live processes either way, so a stale
    registry entry cannot resurrect a dead session on a recycled pane.
    """
    global _PIDS
    reg = Path.home() / ".claude" / "tmux-sessions"
    live: dict[str, str] = {}
    pids: dict[str, int] = {}
    excluded = _excluded_dirs()
    for d in glob.glob("/proc/[0-9]*"):
        try:
            cmd = Path(d, "cmdline").read_bytes().split(b"\0")
            if not cmd or not cmd[0] or os.path.basename(cmd[0].decode(errors="replace")) != "claude":
                continue
            env = Path(d, "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        pane = panes.addr_of_env(dict(e.split(b"=", 1) for e in env if b"=" in e))
        if not pane:
            continue
        if _is_machinery(int(os.path.basename(d)), excluded):
            continue
        # Claude's own record first: it follows /resume and /clear, and it is
        # there when our pane registry lost the entry (claude_sessions).
        from agent_media_core import claude_sessions

        sid = claude_sessions.session_for_pid(int(os.path.basename(d))) or ""
        if not sid:
            m = _UUID.search(b" ".join(cmd).decode(errors="replace"))
            sid = m.group(0) if m else ""
        if not sid and not pane.startswith("herdr:"):
            try:
                parts = (reg / pane.lstrip("%")).read_text().split()
            except OSError:
                parts = []
            # Trust the entry only if it names THIS claude's pid (or no pid at
            # all — legacy rows, kept so long-running sessions aren't dropped).
            if parts and (len(parts) < 2 or parts[1] == os.path.basename(d)):
                sid = parts[0]
        if sid:
            live[sid] = pane
            pids[sid] = int(os.path.basename(d))
    # Codex and pi, each found its own way (see agent_media_core.harnesses).
    from agent_media_core import harnesses

    for run in harnesses.running():
        if run.pid and _is_machinery(run.pid, excluded):
            continue
        if run.pane and run.session not in live:
            live[run.session] = run.pane
            pids[run.session] = run.pid
    # Kept for the memory column of /sessions/state (`_live_states`): this
    # sweep already found each session's process, and finding it again would
    # be a second walk of /proc. Rebound whole, never mutated, so a reader on
    # another thread sees one sweep's answer or the next one's.
    _PIDS = pids
    return live


#: `{session: agent pid}` as the latest `live_sessions` sweep found them.
_PIDS: dict[str, int] = {}


def agent_of(session: str) -> str:
    """Which agent holds `session`: "claude", "codex" or "pi" ("claude" when
    nothing says otherwise — every conversation before these was one)."""
    from agent_media_core import harnesses

    return harnesses.harness_of(session) or harnesses.CLAUDE


def _agent_of_pane(pane: str) -> str:
    if not panes.is_herdr(pane):
        # Hermes runs as the venv's python, so the pane's command name is no
        # answer; its own process says so.
        by_argv = panes.agent_by_argv(
            panes._tmux(["display", "-pt", pane, "#{pane_pid}"]))
        if by_argv:
            return by_argv
    if panes.is_herdr(pane):
        # herdr reports the process running in a pane rather than tmux's
        # "current command"; the field is the same answer by another name.
        cmd = panes.process_name(pane)
    else:
        cmd = panes._tmux(["display", "-pt", pane, "#{pane_current_command}"])
    return cmd if cmd in panes.AGENT_COMMANDS else "claude"


def transcript_cwd(session: str) -> str:
    """The working directory a session ran in, from its own transcript."""
    from agent_media_core import harnesses

    if harnesses.is_hermes(session):
        # Hermes records no cwd for a TUI session, so the pane it is running
        # in answers instead — which is the same directory, while it lasts.
        cwd = harnesses.cwd_of(session)
        if cwd:
            return cwd
        pane = live_sessions().get(session, "")
        return panes.cwd(pane) if pane else ""
    if harnesses.harness_of(session) in (harnesses.CODEX, harnesses.PI):
        return harnesses.cwd_of(session)
    for f in glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session}.jsonl")):
        try:
            with open(f) as fh:
                for line in fh:
                    if '"cwd"' not in line:
                        continue
                    cwd = json.loads(line).get("cwd") or ""
                    if cwd:
                        return cwd
        except (OSError, ValueError):
            continue
    return ""


def session_exists(session: str) -> bool:
    """Whether some harness still has this conversation — so it can be reopened.

    `transcript` answers for the three that write a file; Hermes keeps its in
    a database, so the question is which harness owns the id at all.
    """
    from agent_media_core import harnesses

    return bool(harnesses.harness_of(session))


# --- the ghost prompt -----------------------------------------------------------

_PROMPT_GLYPH = "\u276f"   # ❯ — the input line starts with it
_SGR = re.compile(r"\x1b\[([0-9;]*)m")


def _capture_pane(pane: str) -> str:
    """The bottom of `pane` with its colours, which is where the ghost lives."""
    return panes.capture(pane, lines=40, ansi=True)


def _dim_runs(line: str) -> list[tuple[str, bool]]:
    """`(text, dim)` runs of one captured line, following SGR state along it."""
    out: list[tuple[str, bool]] = []
    dim = False
    pos = 0
    for m in _SGR.finditer(line):
        if m.start() > pos:
            out.append((line[pos:m.start()], dim))
        params = [x for x in m.group(1).split(";") if x] or ["0"]
        i = 0
        while i < len(params):
            p = params[i]
            if p in ("38", "48", "58") and i + 1 < len(params):
                # A colour, whose arguments may well be "2" (truecolor)
                i += 5 if params[i + 1] == "2" else 3
                continue
            if p == "0":
                dim = False
            elif p == "2":
                dim = True
            elif p == "22":
                dim = False
            i += 1
        pos = m.end()
    if pos < len(line):
        out.append((line[pos:], dim))
    return out


def ghost_prompt(pane: str) -> str:
    """Claude Code's suggested next prompt, read off the screen. "" if none.

    When a turn ends the TUI writes a follow-up it thinks you might type next
    onto the input line — dim, accepted with Tab, gone the moment you type.
    It exists nowhere but on that screen: the transcript records that a
    suggestion was accepted, never its words. So it is scraped. The input
    line is the one starting with the prompt glyph; the suggestion is the dim
    run after it, continued onto the lines below while those are dim too
    (a long one wraps). Typed text is not dim, so a half-typed reply reads as
    no suggestion — right, since the ghost has already left the screen.
    """
    cap = _capture_pane(pane)
    if not cap:
        return ""
    lines = cap.split("\n")
    start = next((i for i in range(len(lines) - 1, -1, -1)
                  if _SGR.sub("", lines[i]).lstrip().startswith(_PROMPT_GLYPH)), None)
    if start is None:
        return ""
    words: list[str] = []
    first = True
    for line in lines[start:]:
        runs = _dim_runs(line)
        on_prompt_line = first
        if first:
            # Drop everything up to and including the glyph itself.
            text = "".join(t for t, _ in runs)
            cut = text.index(_PROMPT_GLYPH) + 1
            trimmed: list[tuple[str, bool]] = []
            seen = 0
            for t, d in runs:
                if seen + len(t) <= cut:
                    seen += len(t)
                    continue
                trimmed.append((t[max(0, cut - seen):], d))
                seen += len(t)
            runs = trimmed
            first = False
        plain = "".join(t for t, d in runs if not d).strip("\u00a0 ")
        dim = "".join(t for t, d in runs if d).strip("\u00a0 ")
        if plain and on_prompt_line:
            return ""            # something typed: the ghost has already gone
        if plain or not dim:
            break                # the box's bottom rule, or a bare line
        words.append(dim)
    return " ".join(" ".join(words).split())


def pane_draft(pane: str) -> bool:
    """Is there text half-typed on that session's input line?

    Typing into a pane appends to whatever is already there, so anything this
    sends while a line is being written would join it and be submitted as one
    message. Dim text is the harness's own suggestion, not the listener's, and
    does not count (`ghost_prompt`).
    """
    cap = _capture_pane(pane)
    if not cap:
        return False
    lines = cap.split("\n")
    start = next((i for i in range(len(lines) - 1, -1, -1)
                  if _SGR.sub("", lines[i]).lstrip().startswith(_PROMPT_GLYPH)), None)
    if start is None:
        return False
    runs = _dim_runs(lines[start])
    text = "".join(t for t, _ in runs)
    cut = text.index(_PROMPT_GLYPH) + 1
    seen, typed = 0, []
    for t, dim in runs:
        if seen + len(t) <= cut:
            seen += len(t)
            continue
        if not dim:
            typed.append(t[max(0, cut - seen):])
        seen += len(t)
    return bool("".join(typed).strip("\u00a0 "))


def conversation_pane(session: str) -> str:
    from agent_media_core import conversation

    return conversation.pane_of(session)


# --- the question a session is waiting on ----------------------------------------

#: An option in an agent's dialog: "❯ 1. Yes, and use auto mode". All three
#: agents draw the same shape — a question, then a numbered list, wrapped
#: onto continuation lines when the pane is phone-width.
_OPTION = re.compile(r"^\s*[\u276f\u203a>\u2193\u2191]?\s*(\d+)\.\s+(.*)$")
_SELECTED = re.compile(r"^\s*[\u276f\u203a>]\s*\d+\.\s+\S")
_RULE = re.compile(r"^\s*[\u2500\u2501\u2594\u2581_=]{6,}\s*$")
#: How far above the first option to read the question. A dialog is boxed by
#: a rule, but a cap keeps a missing rule from swallowing the transcript.
_QUESTION_LINES = 8


def parse_dialog(cap: str) -> dict | None:
    """`{"question": ..., "options": [{"n": 1, "label": ...}]}` from a capture.

    Read off the screen because that is where it exists: the harnesses do not
    write their dialogs anywhere a hook can see, and Claude Code's permission
    prompt, Codex's command approval and its hooks-review prompt are all the
    same numbered list.
    """
    lines = [ln.rstrip() for ln in cap.splitlines()]
    numbered = [i for i, ln in enumerate(lines) if _OPTION.match(ln)]
    if not numbered:
        return None
    # The list the selection marker is in, not the first list on screen: a
    # reply above the dialog may itself be a numbered list, and the question
    # belongs to the one the arrow keys are on.
    marked = [i for i in numbered if _SELECTED.match(lines[i])]
    first = numbered[0]
    if marked:
        first = marked[-1]
        while first - 1 in numbered:
            first -= 1
    options: list[list] = []
    indent = 0
    for ln in lines[first:]:
        m = _OPTION.match(ln)
        if m:
            options.append([int(m.group(1)), m.group(2).strip(), ""])
            indent = len(ln) - len(ln.lstrip())
        elif (options and ln.strip() and not _RULE.match(ln)
              and len(ln) - len(ln.lstrip()) > indent):
            # Indented under an option: the rest of a label the pane's width
            # wrapped, or the description a question gives each answer
            # ("Red" / "Warm, bold, high-energy."). The footer below the list
            # ("Press enter to confirm") is not indented, so it is neither.
            options[-1][2] = (options[-1][2] + " " + ln.strip()).strip()
    question: list[str] = []
    for ln in reversed(lines[max(0, first - _QUESTION_LINES):first]):
        if _RULE.match(ln):
            break
        if ln.strip():
            question.append(ln.strip())
    # A list longer than the pane scrolls, and then the screen holds only
    # part of it — the top of the dialog, question and all, may be above the
    # first option that is visible. Say so rather than show a fragment of
    # option 2 as the question: the numbers still answer it, and the phone
    # can offer the desk for the rest.
    # Numbered from 1 with nothing missing, or part of the list is off the
    # screen: Claude's own questions run to four or five answers and a pane
    # this tall shows three of them.
    numbers = [n for n, _label, _detail in options]
    partial = bool(options) and numbers != list(range(1, len(numbers) + 1))
    top_gone = bool(options) and options[0][0] != 1
    return {"question": "" if top_gone else " ".join(reversed(question)),
            "partial": partial,
            "options": [{"n": n, "label": label, "detail": detail}
                        for n, label, detail in options]}


def approval_for(pane: str, agent: str = "claude") -> dict | None:
    """What `pane` is waiting to be told, or None if it is not waiting.

    `key` fingerprints the dialog: an answer carries it back, so a tap that
    arrives after the screen has moved on answers nothing (the question a
    listener read is the question they answered).
    """
    if not pane:
        return None
    cap = panes.strip_ansi(_capture_pane(pane))
    if panes.classify(cap, agent) != "approval":
        return None
    dialog = parse_dialog(cap)
    if not dialog or not dialog["options"]:
        return None
    seed = dialog["question"] + "|" + "|".join(
        f"{o['n']}.{o['label']}/{o['detail']}" for o in dialog["options"])
    # A scrolled dialog is answered by number all the same; the key still
    # follows what was on screen when it was read.
    dialog["key"] = hashlib.sha1(seed.encode()).hexdigest()[:12]
    dialog["agent"] = agent
    return dialog


def _followup(session: str) -> dict | None:
    from agent_media_core.intake._followup import load_followup
    return load_followup(session)


def suggestion_for(session: str, pane: str, last_key: str | None = None) -> str:
    """What to offer as the next line: the real ghost when it fits, else ours.

    The terminal draws the ghost to its own width and cuts it with an ellipsis
    — 28 columns on a phone-sized tmux window — so a truncated ghost is
    replaced by the follow-up the Stop hook wrote for the same reply (see
    core intake/_followup.py). `last_key` is the log's last line: a follow-up
    is only offered for the reply it was written for, so a turn that has
    since moved on does not carry a stale line. None skips that check (the
    /conversation route, which has no log in hand).
    """
    ghost = ghost_prompt(pane) if pane else ""
    if ghost and not ghost.endswith("…"):
        return ghost
    # A cut ghost is never offered: tapping it put the "…" line in the box.
    # The follow-up is written a few seconds after the reply lands, and the
    # app polls, so an empty moment is followed by the whole line.
    fu = _followup(session)
    if not fu:
        return ""
    if last_key is not None and (fu.get("key") or "") != (last_key or ""):
        return ""
    return str(fu.get("text") or "")


def _registry_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get("MEDIA_PANE_REGISTRY_DIR")
                                   or "~/.claude/tmux-sessions"))


def session_of_pane(pane: str, timeout: float = 10.0, agent: str = "claude") -> str:
    """The uuid of the session running in `pane`, or "".

    Codex and pi are asked of harnesses.running(): a codex has a session
    once its first message is in, a pi once its extension has said so.

    A fresh session's uuid is in nobody's argv; the SessionStart hook writes it
    to the pane registry a moment after the TUI is up, so this waits for the
    row and believes it only when the pid it names is a live `claude` — a
    recycled pane id can otherwise hand back the previous tenant.
    """
    if agent != "claude":
        from agent_media_core import harnesses

        deadline = time.monotonic() + timeout
        while True:
            for run in harnesses.running():
                if run.pane == pane and run.harness == agent:
                    return run.session
            if time.monotonic() >= deadline:
                return ""
            time.sleep(0.25)
    row = _registry_dir() / pane.lstrip("%")
    deadline = time.monotonic() + timeout
    while True:
        try:
            parts = row.read_text().split()
        except OSError:
            parts = []
        if parts:
            pid = parts[1] if len(parts) > 1 else ""
            if pid:
                try:
                    cmd0 = Path("/proc", pid, "cmdline").read_bytes().split(b"\0")[0]
                    alive = os.path.basename(cmd0.decode(errors="replace")) == "claude"
                except OSError:
                    alive = False
            else:
                alive = True             # a legacy row without a pid: trust it
            if alive:
                return parts[0]
        if time.monotonic() >= deadline:
            return ""
        time.sleep(0.25)


def project_target(project: str) -> tuple[str, str]:
    """`(tmux session, cwd)` for a fresh session in `project`, or `("", "")`.

    A project is what the library calls a conversation's series: the folder
    above it, named for the tmux session it ran in (`p-agent-media`). The
    directory is the cwd of the newest conversation filed there that still
    has one — a project has no registration of its own, only its history.
    The window opens in the tmux session of that name; the SessionStart hook
    would move it there from the cwd anyway.
    """
    project = (project or "").strip()
    if not project:
        return "", ""
    rows = []
    for f in _manifest_dir().glob("*.json"):
        try:
            data = json.loads(f.read_text())
            at = f.stat().st_mtime
        except (OSError, ValueError):
            continue
        if Path(str(data.get("folder") or "")).parent.name == project:
            rows.append((at, str(data.get("session") or f.stem)))
    for _at, sid in sorted(rows, reverse=True):
        cwd = transcript_cwd(sid)
        if cwd and os.path.isdir(cwd):
            return project, cwd
    return "", ""


def places(limit: int = 6) -> list[dict]:
    """`[{name, path, at}]` — the directories sessions have actually run in.

    What "a new chat in X" can mean, worked out from this host rather than
    from a library: every shelved conversation names its session, and a
    session's transcript names the directory it ran in. Newest first, one row
    per directory, `limit` of them (0 for all).

    This replaces the app asking Audiobookshelf for the series of a library
    that had to be called Conversations, whose series names had to be the
    tmux session names of this particular desk. Another canvas answers this
    with its own directories and the app is none the wiser.
    """
    seen: dict[str, float] = {}

    def take(sid: str, at: float) -> bool:
        """Keep that session's directory. False when the list is full."""
        if limit and len(seen) >= limit:
            return False
        cwd = transcript_cwd(sid)
        if cwd and cwd not in seen and os.path.isdir(cwd):
            seen[cwd] = at
        return True

    # Somewhere a session is running is the most current answer there is, and
    # it beats the shelf whatever the shelf's timestamps say.
    now = time.time()
    for sid in live_sessions():
        if not take(sid, now):
            break
    rows = []
    for f in _manifest_dir().glob("*.json"):
        try:
            rows.append((f.stat().st_mtime, json.loads(f.read_text())))
        except (OSError, ValueError):
            continue
    for at, data in sorted(rows, key=lambda r: -r[0]):
        sid = str(data.get("session") or "")
        if sid and not take(sid, at):
            break
    return [{"name": os.path.basename(path) or path, "path": path, "at": round(at, 3)}
            for path, at in seen.items()]


def targets(bearer: str) -> tuple[bool, dict]:
    """`/targets`: everything a message can be pointed at, in one answer.

    `sessions` are running or lately shelved conversations (the picker's own
    list); `places` are the directories a fresh session can be opened in.
    Gated like `/conversations`.
    """
    ok, detail = auth.may_control_speech(bearer)
    if not ok:
        return False, detail
    return True, {"sessions": sessions_index(), "places": places()}


def _folder_for_session(session: str) -> str:
    for f in sorted(_manifest_dir().glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if str(data.get("session") or f.stem) == session:
            return str(data.get("folder") or "")
    return ""


# --- what each live session is doing, for the app's shelf filters --------------

#: The canvas's pane classes, named for a listener: `input` is a session that
#: has answered and is waiting on you.
_STATE_NAMES = {"working": "working", "input": "waiting", "approval": "approval"}
#: Every open library polls this, and each poll is a /proc sweep plus a
#: capture-pane per live session; a few seconds collapses them to one.
_STATES_TTL_S = 3.0
#: `(monotonic at, (rows, host))`. Anything with a zero time is stale and
#: never unpacked, which is how tests reset it.
_STATES_CACHE: tuple[float, object] = (0.0, [])
_STATES_LOCK = threading.Lock()


def activity_of(session: str, pane: str, *, with_draft: bool = False) -> dict:
    """What a live session is doing: `{"state": "working" | "waiting" |
    "approval" | None}`, and with `with_draft` also `"draft": bool` — text
    half-typed on its input line (`pane_draft`).

    The one place "is this session busy" is answered — `/sessions/state` and
    the idle reaper both ask here — so the source can change underneath (a
    harness's own session list instead of its screen) without either caller
    changing. Today it is read off the pane: `None` means the screen does not
    look like the agent at all (not painted yet, or not an agent), which the
    reaper treats as a reason to leave it alone.
    """
    cls = panes.classify(panes.strip_ansi(_capture_pane(pane)), _agent_of_pane(pane)) \
        if pane else None
    out: dict = {"state": _STATE_NAMES.get(cls, "waiting") if cls else None}
    if with_draft:
        out["draft"] = bool(pane) and pane_draft(pane)
    return out


def _live_states() -> list[dict]:
    tails = {}
    for f in _manifest_dir().glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        tails[str(data.get("session") or f.stem)] = _tail(data.get("folder") or "")
    out = []
    live = live_sessions()
    # Memory per session: the agent process the sweep just found and all of
    # its descendants, in one pass over /proc for every session (procmem).
    pids = _PIDS
    mem = procmem.tree_mem_mb({sid: pids.get(sid) for sid in live})
    for sid, pane in live.items():
        # An unrecognised screen has always been listed as waiting here.
        state = activity_of(sid, pane)["state"] or "waiting"
        out.append({"session": sid, "tail": tails.get(sid, ""),
                    "state": state, "mem_mb": mem.get(sid)})
    return out


def _host(rows: list[dict]) -> dict:
    """The envelope's `host` block: the machine's memory, and how much of it
    the listed sessions hold (the sum of the rows' known `mem_mb`)."""
    return {**procmem.host_mem(),
            "sessions_mem_mb": sum(r.get("mem_mb") or 0 for r in rows)}


def session_states(bearer: str) -> tuple[bool, dict]:
    """`/sessions/state`: working / waiting / approval for every live session.

    Keyed by the item folder's `<author>/<title>` tail as well as the uuid, so
    the app can match its shelf without asking for each item. A session not
    listed is not live. Each row carries `mem_mb`, and the answer a `host`
    block, both computed inside the same cached sweep. Gated like
    `/conversations`.
    """
    global _STATES_CACHE
    ok, detail = auth.may_control_speech(bearer)
    if not ok:
        return False, detail
    with _STATES_LOCK:
        at, cached = _STATES_CACHE
        if time.monotonic() - at > _STATES_TTL_S:
            rows = _live_states()
            cached = (rows, _host(rows))
            _STATES_CACHE = (time.monotonic(), cached)
        rows, host = cached
    return True, {"sessions": rows, "host": host}


# --- which conversation the phone means -----------------------------------------

_SPINNER = re.compile(r"^[\s\u2700-\u27bf\u2600-\u26ff\u25d0-\u25d3\u2b50*·]+")


def _pane_titles() -> dict[str, str]:
    """`{pane: title}` for every Claude Code pane — the session's own title.

    Only Claude's: Codex and pi leave the terminal title as the host name,
    so theirs come from their own session files (`_live_title`).
    """
    out = panes._tmux(["list-panes", "-a", "-F", "#{pane_id}\t#{pane_current_command}\t#{pane_title}"])
    titles = {}
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) >= 3 and f[1] == "claude":
            titles[f[0]] = _SPINNER.sub("", f[2]).strip()
    # herdr keeps the same string as the pane's label, one call per pane —
    # there is no list form that carries it, and there are few of them.
    for addr in _live_herdr_panes():
        title = _SPINNER.sub("", panes.label(addr)).strip()
        if title:
            titles[addr] = title
    return titles


def _live_herdr_panes() -> list[str]:
    """The herdr-held addresses of the sessions running now."""
    return [addr for addr in live_sessions().values() if addr.startswith("herdr:")]


def _live_title(session: str) -> str:
    """A live Codex or pi session's name, or the first thing asked of it."""
    from agent_media_core import harnesses

    title = harnesses.title_of(session) or harnesses.first_prompt(session)
    return title if len(title) <= 60 else title[:59] + "…"


def _recent_conversations(limit: int = 40) -> list[tuple[str, str, float]]:
    """`[(session, title, last modified)]` from the shelf, newest first."""
    rows = []
    for f in _manifest_dir().glob("*.json"):
        try:
            data = json.loads(f.read_text())
            at = f.stat().st_mtime
        except (OSError, ValueError):
            continue
        sid = str(data.get("session") or f.stem)
        title = os.path.basename(str(data.get("folder") or "")).strip()
        if sid and title:
            rows.append((sid, title, at))
    rows.sort(key=lambda r: -r[2])
    return rows[:limit]


def sessions_index() -> list[dict]:
    """What the assistant button can be pointed at: live sessions first, by
    the title on their pane, then recently shelved conversations by their
    folder name. One row per session; a live one that is also on the shelf
    is listed once, live.

    Every row carries `recap`: the latest "where this thread was" summary,
    `{"text", "at", "source"}`, or None — Claude Code's own "while you were
    away" paragraph (`source: "claude"`), or the one the idle reaper wrote
    before resting the session (`"agent-media"`), whichever is newer
    (`recaps.recap_for`). The app uses it as the row's preview line. Cached
    per transcript in `recaps`, so once warm a list of ~44 costs a stat each.

    And `archived`: whether the thread has been archived (`archive`). Archived
    rows stay in the list — the app files them under "Archived" itself, and
    un-archiving from there needs the row.

    And `rested` — `{"at", "reason"}` when the idle reaper closed it, None
    otherwise and always None while live (`rest`) — and `pinned`, whether it
    is kept open against the reaper (`pins`).
    """
    from . import archive, pins, rest

    live = live_sessions()
    titles = _pane_titles()
    flags = archive.archived()
    pinned = pins.pinned()
    marks = rest.rested()
    seen: set[str] = set()
    out = []
    for sid, pane in live.items():
        title = titles.get(pane) or ("" if pane in titles else _live_title(sid))
        if not title:
            continue
        seen.add(sid)
        out.append({"session": sid, "title": title, "live": True, "pane": pane,
                    "recap": recaps.recap_for(sid), "archived": sid in flags,
                    "rested": None, "pinned": sid in pinned})
    for sid, title, at in _recent_conversations():
        if sid in seen:
            continue
        seen.add(sid)
        out.append({"session": sid, "title": title, "live": False, "pane": None, "at": at,
                    "recap": recaps.recap_for(sid), "archived": sid in flags,
                    "rested": rest.row_mark(sid, False, marks), "pinned": sid in pinned})
    return out
