"""Reply from the player: an Audiobookshelf listener types back into a session.

You are listening to a past conversation in Sasonica (the ABS app fork). A box
under the player takes a line of text and puts it into the Claude Code session
that produced what you are hearing — reviving it in a tmux window if it has
since ended.

The design, and the arguments for it, are in
`docs/proposals/2026-09-04-reply-from-the-player.md`. Two of the three things
it settles are identity and live in `auth_abs`; the third is implemented here:

* **A dead session is revived, not refused.** In a background window, in the
  attached tmux client, because Claude Code's TUI will not start without one.

Everything that puts keys into a pane goes through this module: a reply, a
fresh session from the assistant button (`ask`), resuming and closing a
session, answering a dialog, `/rename`. Typing a message is the one seam
`_send_to_pane`, which the tests replace with a recorder.

Moved out of the canvas's reply.py.

Config (env):
  MEDIA_REPLY_TMUX    tmux session to open revived windows in (default: the
                      one with an attached client)
  MEDIA_ASK_SESSION   amux session name whose registration (directory, flags)
                      a fresh session started from the phone copies
                      (default: scratch); MEDIA_ASK_TMUX / MEDIA_ASK_CWD /
                      MEDIA_ASK_FLAGS override its parts
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import auth, panes, sessions

log = logging.getLogger("agent-media.server.send")


# How long to wait for a revived pane's TUI to accept input, and how often to
# look. Claude Code takes a few seconds to paint; typing before it is up drops
# the text on the floor, so this is a readiness probe, not a sleep.
READY_TIMEOUT_S = float(os.environ.get("MEDIA_REPLY_READY_TIMEOUT") or 45.0)
READY_POLL_S = 0.5


def send_rename(session: str, title: str) -> str:
    """Have Claude Code rename its own session. "" when it did, else why not.

    `/rename <title>` rather than `tmux rename-window`: the name then belongs
    to the session — the prompt bar, `/resume`, and this host's window names,
    which tmux builds from the pane's label rather than from a name set by
    hand. Renaming the window directly also turned that window's
    automatic-rename off, so every later name it should have picked up was
    lost; this turns it back on.
    """
    pane = sessions.conversation_pane(session)
    if not pane:
        return "no pane: the session is not running"
    if sessions.pane_draft(pane):
        # Ours would be appended to what is being typed and sent as one line.
        return "something is being typed there"
    err = panes.send(pane, f"/rename {title}")
    if err:
        return err
    panes._tmux(["set-window-option", "-t", pane, "automatic-rename", "on"])
    return ""


def answer(session: str, choice: int, key: str, bearer: str) -> tuple[bool, dict]:
    """Answer the dialog a session is holding. `(ok, detail)`.

    A number and Enter, never text: the digit moves the selection and Enter
    takes it (measured on all three). Refused unless that very dialog is
    still up — same options, same words — so this cannot be turned into a
    way of pressing keys into whatever a pane has moved on to.
    """
    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    pane = sessions.live_sessions().get(session, "")
    if not pane or not panes.alive(pane):
        return False, {"error": f"session {session[:8]} is not live", "status": 404}
    agent = sessions._agent_of_pane(pane)
    dialog = sessions.approval_for(pane, agent)
    if not dialog:
        return False, {"error": "that session is not waiting on a question", "status": 409}
    if key and key != dialog["key"]:
        return False, {"error": "the question has changed", "status": 409,
                       "approval": dialog}
    if choice not in {o["n"] for o in dialog["options"]}:
        return False, {"error": f"no option {choice}", "status": 400, "approval": dialog}
    if panes.is_herdr(pane):
        panes._run(["herdr", "pane", "send-keys", panes.herdr_pane(pane), str(choice)])
        time.sleep(0.15)
        panes._run(["herdr", "pane", "send-keys", panes.herdr_pane(pane), "enter"])
    else:
        panes._tmux(["send-keys", "-t", pane, str(choice)])
        time.sleep(0.15)
        panes._tmux(["send-keys", "-t", pane, "Enter"])
    # Say whether it took: the answer is worth reporting honestly, and a
    # dialog still up after it means the keys went nowhere.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        time.sleep(0.4)
        now = sessions.approval_for(pane, agent)
        if not now or now["key"] != dialog["key"]:
            return True, {"session": session, "pane": pane, "answered": choice,
                          "label": next(o["label"] for o in dialog["options"] if o["n"] == choice),
                          "waiting": bool(now), "approval": now}
    return False, {"error": "the question is still on screen", "status": 504,
                   "session": session, "pane": pane, "approval": dialog}


# --- reviving a session that has ended -----------------------------------------


def attached_session() -> str:
    """A tmux session with a client attached to it, or "".

    Claude Code's TUI will not start without one — a detached `new-session`
    gives you a pane that dies on startup, which cost a previous session an
    afternoon. So a revived window goes into a session someone is looking at.
    """
    want = (os.environ.get("MEDIA_REPLY_TMUX") or "").strip()
    if want:
        return want
    clients = panes._tmux(["list-clients", "-F", "#{client_session}"])
    return clients.splitlines()[0] if clients else ""


# Resuming a long session opens a modal before the TUI: "Resume from summary
# (recommended) / Resume full session as-is / Don't ask me again". A revived
# window sits on it forever otherwise. Enter takes the highlighted recommended
# option, which is also the right one here — a reply does not need the whole
# transcript re-read, and the full resume is what blows through usage limits.
_RESUME_PROMPT = re.compile(r"Resume from summary|Resume full session")


def pane_ready(pane: str, agent: str = "claude") -> bool:
    """Whether the agent's TUI in `pane` is painted and taking input.

    Answers the resume-choice modal if it is up. Pressing Enter into a pane is
    exactly what `send_input` refuses to do blind — the difference is that this
    window is one we opened seconds ago and the text on it is matched first.
    """
    cap = panes.strip_ansi(panes.capture(pane, lines=40, ansi=False))
    state = panes.classify(cap, agent)
    # Claude's resume modal is answered below; Codex's startup prompts (hooks
    # to trust, a directory to trust) are the person's to answer, and text
    # typed into one is lost — so for the others only a waiting composer is
    # ready.
    if state is not None and (agent == "claude" or state == "input"):
        return True
    if agent == "claude" and _RESUME_PROMPT.search(cap):
        if panes.is_herdr(pane):
            panes._run(["herdr", "pane", "send-keys", panes.herdr_pane(pane), "enter"])
        else:
            panes._tmux(["send-keys", "-t", pane, "Enter"])
    return False


# A tmux session with nobody attached cannot host a Claude Code TUI (see
# `attached_session`), and the scratch session a phone-started chat goes into
# is exactly the kind nobody is looking at. So a client is held on it:
# `script` gives `tmux` the tty it insists on, and `new-session -A` creates
# the session attached from its first moment — which matters twice over. A
# session born detached loses its only window to the after-new-session hook
# (tmux-claude-resume respawns an unattended window as a `claude --resume`,
# which then dies for want of a client, and the empty session goes with it);
# the hook skips sessions that have a client. And the holder lives in its own
# transient systemd unit, not this process: the canvas restarts on every
# deploy, and a chat started from the phone must not die with it.
_HOLD_UNIT = "agent-media-tmux-hold-{host}"
_HOLDERS: dict[str, subprocess.Popen] = {}
_HOLDER_LOCK = threading.Lock()


def _has_client(host: str) -> bool:
    return bool(panes._tmux(["list-clients", "-t", f"={host}", "-F", "#{client_tty}"]))


def _holder_argv(host: str, cwd: str) -> list[str]:
    # $SHELL runs `script -c`, and under the user manager that is zsh, whose
    # `=word` expansion eats a `-t =name` target — so the shell is pinned and
    # the target is `-s name`, which needs no exact-match prefix.
    return ["script", "-qfc", f"tmux new-session -A -s {shlex.quote(host)} -c {shlex.quote(cwd)}",
            "/dev/null"]


def hold_client(host: str, cwd: str = "") -> bool:
    """Keep a client attached to tmux session `host`, creating it if need be.

    Whether one is attached by the time this returns.
    """
    if _has_client(host):
        return True
    cwd = cwd or os.path.expanduser("~")
    env = {"TERM": "xterm-256color", "SHELL": "/bin/sh"}
    with _HOLDER_LOCK:
        held = _HOLDERS.get(host)
        if held is None or held.poll() is not None:
            unit = _HOLD_UNIT.format(host=re.sub(r"[^A-Za-z0-9_.-]", "-", host))
            argv = _holder_argv(host, cwd)
            spawned = False
            if shutil.which("systemd-run"):
                # A stopped unit of that name may linger failed; clear it. And
                # a *live* one is no use either: its client has drifted. When
                # the session it held is destroyed, `detach-on-destroy off`
                # moves the client to some other session instead of ending it,
                # so the unit stays "active" holding the wrong thing and a
                # second holder under the same name is refused. Stop it; a
                # fresh one is spawned below (seen 2026-09-16: six-day-old
                # holder parked on p-agent-media, every phone ask refused).
                subprocess.run(["systemctl", "--user", "stop", f"{unit}.service"],
                               capture_output=True, check=False)
                subprocess.run(["systemctl", "--user", "reset-failed", f"{unit}.service"],
                               capture_output=True, check=False)
                r = subprocess.run(["systemd-run", "--user", "--collect", f"--unit={unit}",
                                    *[f"--setenv={k}={v}" for k, v in env.items()], *argv],
                                   capture_output=True, text=True, check=False)
                spawned = r.returncode == 0
                if not spawned:
                    log.warning("reply: systemd-run holder failed (%s); holding in-process",
                                r.stderr.strip()[:200])
            if not spawned:
                try:
                    _HOLDERS[host] = subprocess.Popen(
                        argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True,
                        env={**os.environ, **env})
                except OSError as e:
                    log.warning("reply: could not hold a client on %s (%s)", host, e)
                    return False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _has_client(host):
            return True
        time.sleep(0.2)
    return False


def ensure_host(host: str, cwd: str) -> bool:
    """Make tmux session `host` exist, with a client on it. Whether it does."""
    return hold_client(host, cwd)


def _claude_bin(name: str = "claude") -> str:
    """Where `claude` (or `codex`, `pi`, `hermes`) is, by absolute path.

    A window's command runs with whatever PATH the tmux session was born
    with, and a session the canvas made from under systemd has the user
    manager's — no ~/.local/bin, no bun, no npm — so `claude` was "not found"
    in a window that looked exactly like a working one. `harnesses.program`
    is that enriched lookup, shared with the installer.
    """
    from agent_media_core import harnesses

    return harnesses.program(name) or name


def open_window(session: str, cwd: str, *, resume: bool, host: str = "",
                flags: tuple[str, ...] | list[str] = (),
                agent: str = "claude") -> tuple[str, str]:
    """Open a background tmux window running an agent. `(pane, error)`.

    `agent` is "claude", "codex" or "pi". A fresh pi is started with
    `session` as its id when one is given; the others choose their own.

    `host` names the tmux session to open it in; by default the one someone is
    attached to. `flags` go to `claude` (a fresh session may want
    `--dangerously-skip-permissions`; a revived one wants nothing).

    Background (`-d`) on purpose: this is triggered from the phone, and a
    window that steals the desk's focus mid-task is a worse answer than one
    that waits to be found. The app is handed the pane id so it can offer a
    link back to it.

    Two things observed doing this for real, neither a fault:

    * The window does not stay where we put it — a SessionStart hook moves it
      into the session named for its project. The pane id survives the move, so
      everything downstream still works; do not "fix" the target.
    * Answering the resume modal means resuming from a summary, and that runs a
      compaction first. On a large transcript the reply sits in Claude Code's
      own queue for a minute or two before it is read. That is why the caller
      is told `opened: true` — "opening" is a truer thing to show than "sent".
    """
    if resume:
        # Never a second copy of a running session: two writers interleave
        # their turns into one transcript. Live detection has missed a running
        # session before (a lost registry entry), and a reply from the phone
        # then opened a duplicate beside it; Claude's own record is the check.
        from agent_media_core import claude_sessions, harnesses

        others = [(r.pid, r.session, r.pane) for r in harnesses.running()]
        for pid, sid, where in [*claude_sessions.running(), *others]:
            if sid == session:
                return where, (f"session {session[:8]} is already running"
                               + (f" in {where}" if where else f" outside tmux (pid {pid})"))
    cwd = cwd or os.path.expanduser("~")
    if host:
        if not ensure_host(host, cwd):
            return "", f"could not get a client onto tmux session {host!r}"
    else:
        host = attached_session()
        if not host:
            return "", "no attached tmux session to open a window in"
    from agent_media_core import harnesses

    # The agent's own directory goes first on PATH: pi is a node script, and
    # a window born under systemd has no node on its PATH (fnm keeps node
    # next to the agents it installed).
    exe = _claude_bin(agent)
    cmd = "exec env -u ANTHROPIC_API_KEY"
    if os.path.isabs(exe):
        cmd += f" PATH={shlex.quote(os.path.dirname(exe))}:\"$PATH\""
    cmd += f" {shlex.quote(exe)}"
    args = (harnesses.resume_argv(agent, session) if resume
            else harnesses.fresh_argv(agent, session))
    if args:
        cmd += " " + shlex.join(args)
    if flags:
        cmd += " " + shlex.join(list(flags))
    # "host:" not "host": a bare name is a target *window*, and tmux happily
    # resolves it into some other session's window (verified the hard way).
    pane = panes._tmux(["new-window", "-d", "-t", f"{host}:", "-c", cwd,
                  "-P", "-F", "#{pane_id}", cmd])
    if not pane:
        return "", "tmux could not open a window"
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        time.sleep(READY_POLL_S)
        if pane_ready(pane, agent):
            return pane, ""
    return pane, f"{pane} did not come up within {READY_TIMEOUT_S:.0f}s"


def focus(pane: str) -> tuple[bool, str]:
    """Bring the attached client to `pane` — the app's "opened in %23" link.

    Single user, so pulling the desk's screen somewhere from the phone is a
    feature. Only panes hosting Claude Code are eligible, so this cannot be
    used to go rummaging through someone's shells.
    """
    if pane not in {p["pane"] for p in panes.tmux_agent_panes() + panes.herdr_agent_panes()}:
        return False, f"not a live agent pane: {pane!r}"
    if panes.is_herdr(pane):
        # herdr focuses a pane by id, workspace and tab included.
        return (True, pane) if panes.focus(pane) else (False, f"pane {pane} is gone")
    sess = panes._tmux(["display", "-pt", pane, "#{session_name}"])
    win = panes._tmux(["display", "-pt", pane, "#{window_id}"])
    if not sess or not win:
        return False, f"pane {pane} is gone"
    panes._tmux(["switch-client", "-t", sess])
    panes._tmux(["select-window", "-t", win])
    panes._tmux(["select-pane", "-t", pane])
    return True, pane


# --- a fresh session, from the phone -------------------------------------------

def ask_target() -> tuple[str, str, list[str]]:
    """`(tmux session, cwd, claude flags)` for a session started from the phone.

    Copied from the amux registration named by MEDIA_ASK_SESSION (default
    `scratch`) — the same directory and flags `amux start scratch` would use,
    in the tmux session amux would put it in — so where a phone-started chat
    lands is set in one place, with amux. Each part can be overridden.
    """
    name = (os.environ.get("MEDIA_ASK_SESSION") or "scratch").strip()
    cwd, flags = "", ""
    env = Path(os.path.expanduser(os.environ.get("CC_HOME") or "~/.amux")) / "sessions" / f"{name}.env"
    try:
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if k == "CC_DIR":
                cwd = v
            elif k == "CC_FLAGS":
                flags = v
    except OSError:
        pass
    host = (os.environ.get("MEDIA_ASK_TMUX") or "").strip() or f"amux-{name}"
    cwd = os.path.expanduser((os.environ.get("MEDIA_ASK_CWD") or "").strip() or cwd or "~")
    flags_s = os.environ.get("MEDIA_ASK_FLAGS")
    argv = shlex.split(flags_s if flags_s is not None else flags)
    return host, cwd, argv


def _settle(pane: str, timeout: float = 5.0) -> None:
    """Wait until the screen in `pane` stops changing (or `timeout` passes).

    `pane_ready` says yes at the first sight of Claude Code's chrome, and the
    TUI is still painting for a moment after that: a message typed then is
    taken but its Enter is not, and it sits in the box unsent. Two identical
    captures a beat apart is the TUI at rest.
    """
    last = sessions._capture_pane(pane)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.7)
        now = sessions._capture_pane(pane)
        if now == last:
            return
        last = now


#: Where each agent's composer starts: the text after it is what is typed.
_COMPOSER = {"claude": sessions._PROMPT_GLYPH, "codex": "\u203a"}   # ❯, ›

#: How long to watch the composer for the line to go, after each press. The
#: evidence here is a screen capture rather than a transcript, so the window
#: is much shorter than the one `conversation.submit` defaults to.
_SUBMIT_SETTLE_S = 1.5


def _unsent(pane: str, head: str, agent: str, window: float) -> bool:
    """Whether `head` is still sitting in `pane`'s composer for all of `window`.

    False the moment the box lets go of it — that is the line being taken, and
    it is the signal worth trusting: `_classify_agent` needs the footer, which
    a narrow pane truncates away, while an empty composer is an empty composer
    at any width.

    Fails open. A capture that comes back empty reads as taken, because the
    alternative is hammering Enter at a pane we cannot see.
    """
    deadline = time.monotonic() + window
    while True:
        time.sleep(0.5)
        cap = panes.strip_ansi(sessions._capture_pane(pane))
        if panes.classify(cap, agent) == "working":
            return False
        if agent == "pi":
            # pi's composer is the box between the last two rules.
            rules = [i for i, ln in enumerate(cap.splitlines()) if re.fullmatch(r"\s*─{8,}\s*", ln)]
            box = cap.splitlines()[rules[-2] + 1:rules[-1]] if len(rules) >= 2 else []
            if head not in " ".join(" ".join(box).split()):
                return False
        else:
            flat = " ".join(cap.split())
            i = flat.rfind(_COMPOSER.get(agent, sessions._PROMPT_GLYPH))
            if i < 0 or head not in flat[i:]:
                return False
        if time.monotonic() >= deadline:
            return True


def _ensure_submitted(pane: str, text: str, timeout: float = 3.0,
                      agent: str = "claude") -> bool:
    """Press Enter until `text` leaves the input box. True if it did.

    The loop and the giving up belong to `conversation.submit`, which `media
    ask` needs for the same reason. What is local to a surface is the
    evidence: it has a pane to look at rather than a session whose transcript
    it could read, so "still in the box" — the first words of the message
    after the prompt glyph, with no sign of a turn in progress — is what it
    reports.
    """
    from agent_media_core import conversation as conv

    head = " ".join(text.split())[:24]
    return conv.submit(pane, lambda window: not _unsent(pane, head, agent, window),
                       first=timeout, settle=_SUBMIT_SETTLE_S)


def _unsent_error(pane: str, session: str = "") -> dict:
    """What to say when the words are in the box and the agent never took them.

    Neither a transport failure nor a send: the words are in the composer and
    one Enter would still deliver them. What must not happen is the turn being
    shelved anyway — a conversation that grows a question with no answer
    coming is exactly the three-dots-forever the phone showed. So the callers
    return this instead of recording, and the surface gets a sentence it can
    put on screen in place of a typing indicator.
    """
    return {"error": f"typed into {pane} but the session did not take it — "
                     "press Enter in that pane to send it",
            "pane": pane, "submitted": False,
            "session": session or None, "status": 502}


def _send_to_pane(pane: str, text: str) -> str:
    """Type `text` + Enter into a pane, tmux's or herdr's (amux's
    literal-then-Enter timing, which Claude Code's input buffering needs).
    Returns "" or an error.

    The one seam every message into a conversation goes through — tests
    replace it to record what would have been typed."""
    return panes.send(pane, text)



def ask(text: str, bearer: str, *, quote: str = "", project: str = "",
        agent: str = "", cwd: str = "") -> tuple[bool, dict]:
    """Start a fresh session with `text` as its first message.

    What the phone's assistant button does. Nothing to resume and no item yet:
    the window opens in the scratch session, the words are typed in, and the
    listener's turn is shelved against the new session's uuid so the
    conversation appears in the library once its first reply is spoken. The
    app is told the uuid and polls `/conversation?session=` for the item.
    `project` (a series name) opens it in that project's directory instead,
    and `cwd` (a directory, as `/targets` hands them out) says the same thing
    without the library's naming convention in the middle.
    `agent` picks Claude Code (the default, or MEDIA_ASK_AGENT), Codex or pi.
    """
    text = " ".join((text or "").split())
    if not text:
        return False, {"error": "empty message"}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    agent = (agent or os.environ.get("MEDIA_ASK_AGENT") or "claude").strip().lower()
    if agent not in panes.AGENT_COMMANDS:
        return False, {"error": f"unknown agent {agent!r}", "status": 400}
    where = (cwd or "").strip()
    host, cwd, flags = ask_target()
    if agent != "claude":
        flags = []          # amux's flags are claude's (--dangerously-skip-permissions)
    if where:
        # A directory named outright. Only somewhere a session has actually
        # run: this opens a shell there, so it is not the phone's to choose
        # freely.
        if where not in {p["path"] for p in sessions.places(limit=0)}:
            return False, {"error": f"no session has run in {where!r}", "status": 404}
        host, cwd = os.path.basename(where), where
    elif project:
        host, cwd = sessions.project_target(project)
        if not cwd:
            return False, {"error": f"no directory known for project {project!r}", "status": 404}
    # pi takes its id up front; the others are asked for theirs.
    fixed = str(uuid.uuid4()) if agent == "pi" else ""
    pane, err = open_window(fixed, cwd, resume=False, host=host, flags=flags, agent=agent)
    if err:
        return False, {"error": err, "pane": pane or None}
    # A codex has no session until its first message is in, so it is asked
    # after the send.
    session = fixed or (sessions.session_of_pane(pane) if agent == "claude" else "")
    _settle(pane)
    body = compose(text, quote)
    send_err = _send_to_pane(pane, body)
    if send_err:
        return False, {"error": send_err, "session": session or None, "pane": pane}
    took = _ensure_submitted(pane, body, agent=agent)
    if not session:
        session = sessions.session_of_pane(pane, agent=agent)
    if not took:
        return False, _unsent_error(pane, session)
    if session:
        _record_turn(session, text, pane)
    return True, {"session": session or None, "pane": pane, "opened": True,
                  "fresh": True, "tmux": host, "agent": agent, "submitted": True}


# --- managing the session behind a conversation ---------------------------------

def _retag(session: str) -> None:
    """Reconcile the live tag now rather than at the next sweep."""
    try:
        from agent_media_core import book_tracks

        threading.Thread(target=book_tracks.sync_tags, daemon=True).start()
    except Exception:  # noqa: BLE001 — the sweep will get it
        pass


def session_resume(session: str, bearer: str) -> tuple[bool, dict]:
    """Bring a conversation's session back in a tmux window, saying nothing.

    What "resume" in the app does: the same revive a reply performs, without
    a reply — the listener wants the terminal back, or wants the thread live
    before speaking into it. Already live: says where it is.
    """
    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    pane = sessions.live_sessions().get(session, "")
    if pane and panes.alive(pane):
        return True, {"session": session, "pane": pane, "live": True, "opened": False}
    if not sessions.session_exists(session):
        return False, {"error": f"session {session[:8]} has no transcript to resume", "status": 404}
    pane, err = open_window(session, sessions.transcript_cwd(session), resume=True,
                            agent=sessions.agent_of(session))
    if err:
        return False, {"error": err, "pane": pane or None}
    _retag(session)
    return True, {"session": session, "pane": pane, "live": True, "opened": True}


def session_close(session: str, bearer: str) -> tuple[bool, dict]:
    """End a conversation's session: close the pane it runs in.

    Only a pane that hosts this very session is touched — never a shell, and
    never a pane that has since been recycled for something else. Claude Code
    ends the session cleanly on the pane closing (SessionEnd fires), and the
    transcript stays, so this is undone by `session_resume`.
    """
    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    pane = sessions.live_sessions().get(session, "")
    if not pane:
        return True, {"session": session, "live": False, "closed": False}
    if not panes._tmux(["kill-pane", "-t", pane]) and panes._tmux(["display", "-pt", pane, "#{pane_id}"]):
        return False, {"error": f"could not close {pane}", "pane": pane}
    _retag(session)
    return True, {"session": session, "pane": pane, "live": False, "closed": True}


# --- the whole move -----------------------------------------------------------

# Quote and reply go in on ONE line. `send-keys` types literally and then
# presses Enter, so an embedded newline would submit half a message; a quoted
# turn is context, not a document, and one line carries it.
_QUOTE_LIMIT = 160


def compose(text: str, quote: str = "") -> str:
    # The reply is flattened too, not just the quote: the box grows to several
    # rows now, and shift+enter puts a real newline in it. `send-keys` types
    # literally and then presses Enter, so a newline mid-message would submit
    # the first half and leave the rest sitting in the composer.
    text = " ".join((text or "").split())
    quote = " ".join((quote or "").split())
    if not quote:
        return text
    if len(quote) > _QUOTE_LIMIT:
        quote = quote[:_QUOTE_LIMIT - 1] + "…"
    return f'Re: "{quote}" — {text}'


def _record_turn(session: str, text: str, pane: str = "") -> None:
    """Put the listener's own words into the conversation, in the background.

    Rendering takes a second or two and the reply has already been delivered,
    so this must not sit in front of the response — the box would look stuck
    for no reason the user could see. Failures are logged and dropped: the
    words reached the session either way. `pane` names where the words went,
    so the turn carries its tmux session like a spoken one — the workspace a
    conversation is filed under is read off its turns, and a fresh session's
    first export may hold only this turn.
    """
    where = {}
    if pane:
        at = panes.where(pane)
        if at.get("session"):
            where = {"source_tmux_session": at["session"], "source_pane": pane}
            if at.get("source") != "tmux":
                where["source_kind"] = at["source"]

    def run() -> None:
        try:
            from agent_media_core import book_tracks, slash

            # The same rule the prompt hook uses. Without it this path recorded
            # and spoke every slash command typed into the box while the
            # terminal dropped them all — one conversation kept a turn that
            # said only "You: /".
            if slash.parse(text) is not None:
                cmd = slash.turn_for(text, session)
                if cmd is None:
                    return
                book_tracks.record_listener_turn(session, cmd["text"],
                                                 extras={**where, "command": cmd})
                return
            book_tracks.record_listener_turn(session, text, extras=where or None)
        except Exception as e:  # noqa: BLE001 — the reply already landed
            print(f"reply: could not shelve the listener's turn ({e})",
                  file=sys.stderr)

    threading.Thread(target=run, daemon=True).start()


def reply(item: str, text: str, bearer: str, *, quote: str = "",
          mode: str = "continue", session: str = "") -> tuple[bool, dict]:
    """Put `text` into `session`, or into the session behind ABS item `item`.

    `session` is the v1 form (server-contract.md §10) and wins when both are
    given: it is the thread's own id, and asking ABS which session an item
    means is a network call that can only agree with it or be wrong. `item`
    stays until the ABS exit.

    `continue` types into the live pane, reviving the session in a background
    window if it has ended. `branch` always opens a fresh session in the same
    working directory, seeded with the quoted line — the cheap version of
    forking a conversation, which Claude Code cannot really do (see the
    proposal: a true fork means truncating an undocumented transcript format).
    """
    text = (text or "").strip()
    if not text:
        return False, {"error": "empty reply"}
    session = (session or "").strip()
    if session and not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if session:
        # A session id nothing knows — not running, no transcript — is the
        # same answer an item with no session behind it gets: not there.
        # Checked here rather than left to `deliver`, because `branch` would
        # otherwise open a fresh session in no particular directory.
        if not sessions.live_sessions().get(session) and not sessions.session_exists(session):
            return False, {"error": f"no such session {session[:8]}", "status": 404}
    else:
        session, err = sessions.session_for_item(item, bearer)
        if not session:
            return False, {"error": err, "status": 404}
    # `body` is typed on one line (compose flattens it); `text` is recorded
    # with the breaks the reply box had, so the transcript keeps them.
    body = compose(text, quote)

    if mode == "branch":
        agent = sessions.agent_of(session)
        pane, err = open_window("", sessions.transcript_cwd(session), resume=False, agent=agent)
        if err:
            return False, {"error": err, "pane": pane or None}
        send_err = _send_to_pane(pane, body)
        if send_err:
            return False, {"session": session, "pane": pane, "opened": True,
                           "branched": True, "error": send_err}
        if not _ensure_submitted(pane, body, agent=agent):
            return False, {**_unsent_error(pane, session), "branched": True}
        _record_turn(session, text, pane)
        return True, {"session": session, "pane": pane, "opened": True,
                      "branched": True, "submitted": True}

    return deliver(session, body, text)


def deliver(session: str, body: str, text: str) -> tuple[bool, dict]:
    """Put `body` into `session`'s live pane, reviving it if it has ended.

    `text` is the listener's own words, shelved as their turn; `body` is what
    is typed (the quote rides along in it). Shared by a reply from a
    conversation's page and a reply the assistant button routed here.
    """
    pane = sessions.live_sessions().get(session, "")
    opened = False
    agent = sessions.agent_of(session)
    if pane and panes.alive(pane):
        pass
    elif not sessions.session_exists(session):
        # No transcript: nothing to revive, and reviving into a fresh session
        # would silently answer as someone else.
        return False, {"error": f"session {session[:8]} has no transcript to resume"}
    else:
        pane, err = open_window(session, sessions.transcript_cwd(session), resume=True, agent=agent)
        if err:
            return False, {"error": err, "pane": pane or None}
        opened = True
    send_err = _send_to_pane(pane, body)
    if send_err:
        return False, {"error": send_err, "session": session, "pane": pane}
    # A long reply's Enter can arrive while the TUI is still taking the text
    # and be lost — the words sat in the box of a live session, unsent, until
    # someone pressed Enter by hand. Look, press, and look again.
    if not _ensure_submitted(pane, body, agent=agent):
        return False, {**_unsent_error(pane, session), "opened": opened}
    _record_turn(session, text, pane)
    return True, {"session": session, "pane": pane, "opened": opened,
                  "submitted": True}
