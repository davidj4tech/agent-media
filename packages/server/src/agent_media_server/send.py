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

Since 22 Sep 2026 the gated entry points here (`reply`, `deliver`, `ask`,
`answer`, `session_resume`, `session_close`) ask the Driver seam (driver/)
which driver owns the session. The pane driver calls back into the `_…_pane`
functions below, which are the code these routes always ran; a headless
session (MEDIA_HEADLESS, sessiond.py) never reaches them.

Moved out of the canvas's reply.py.

Config (env):
  MEDIA_LAYOUT        "default" | "projects-per-tmux-session" (else config.toml,
                      else detected) — agent_media_core/layout.py answers every
                      "which tmux session" below
  MEDIA_REPLY_TMUX    tmux session to open revived windows in (default: the
                      one with an attached client; `sasonica` in the default
                      layout)
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

    A headless session (MEDIA_HEADLESS) has no pane: it gets the same
    `/rename` as a stream-json message through sessiond, which Claude Code
    takes under `-p`; a parked one reads the name from its transcript on its
    next resume.
    """
    from . import driver

    if driver.owned_headless(session):
        return driver.headless_driver().rename(session, title)
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


def answer(session: str, choice: int, key: str, bearer: str, *,
           request_id: str = "", decision: str = "", answers=None,
           message: str = "") -> tuple[bool, dict]:
    """Answer what a session is waiting on. `(ok, detail)`.

    Two forms, both through the driver that owns the session (driver/): the
    numbered one — `choice` and the dialog's `key` — which every session
    takes, and the structured one — `request_id` and `decision` ("allow" |
    "deny", with `answers` for a question) — which a headless session takes
    (server-contract.md §6.4).
    """
    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    from . import driver

    request: dict = {"choice": choice, "key": key}
    if request_id or decision or answers is not None:
        request = {"request_id": request_id, "decision": decision, "answers": answers,
                   "message": message, "key": key}
    return driver.for_session(session).answer(session, request)


def _answer_pane(session: str, choice: int, key: str, answers=None) -> tuple[bool, dict]:
    """The pane driver's answer: a number and Enter — or, for a question
    (AskUserQuestion), the structured `answers` given key by key.

    The digit moves the selection and Enter takes it (measured on all three).
    Refused unless that very dialog is still up — same options, same words —
    so this cannot be turned into a way of pressing keys into whatever a pane
    has moved on to. A question's words are typed only onto its free-text
    row, and only once the screen shows the cursor there (asks.drive).
    """
    pane = sessions.live_sessions().get(session, "")
    if not pane or not panes.alive(pane):
        return False, {"error": f"session {session[:8]} is not live", "status": 404}
    agent = sessions._agent_of_pane(pane)
    dialog = sessions.approval_for(pane, agent, session)
    if not dialog:
        return False, {"error": "that session is not waiting on a question", "status": 409}
    if key and key != dialog["key"]:
        return False, {"error": "the question has changed", "status": 409,
                       "approval": dialog}
    if dialog.get("kind") == "question" and (answers is not None or not dialog.get("review")):
        # (The review page by number is its own list: 1 sends, 2 cancels.)
        qs = dialog.get("questions") or []
        if answers is None and (dialog.get("multiSelect") or len(qs) > 1):
            # A number cannot answer several questions, and on a multi-select
            # the digit only ticks a box: give it as the one box ticked.
            if len(qs) != 1 or choice not in {o["n"] for o in qs[0]["options"]}:
                return False, {"error": "this question takes answers, not a number",
                               "status": 400, "approval": dialog}
            answers = [{"question_index": 0, "selected": [choice]}]
        if answers is not None:
            return _answer_question(session, pane, agent, dialog, answers)
    elif answers is not None:
        return False, {"error": "this is not a question: answer it by number (choice and key)",
                       "status": 400, "approval": dialog}
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
        now = sessions.approval_for(pane, agent, session)
        if not now or now["key"] != dialog["key"]:
            return True, {"session": session, "pane": pane, "answered": choice,
                          "label": next(o["label"] for o in dialog["options"] if o["n"] == choice),
                          "waiting": bool(now), "approval": now}
    return False, {"error": "the question is still on screen", "status": 504,
                   "session": session, "pane": pane, "approval": dialog}


def _answer_question(session: str, pane: str, agent: str, dialog: dict,
                     answers) -> tuple[bool, dict]:
    """Give an AskUserQuestion its structured answers (asks.drive), then check
    the dialog has gone, as the numbered path does."""
    from . import asks

    qs = dialog.get("questions") or []
    if dialog.get("partial") or any(not q["question"] or not q["options"] for q in qs):
        # Without the hook's copy only the tab on screen is known, and a
        # scrolled list hides some of its options.
        return False, {"error": "only part of this question is on screen: answer it at the desk",
                       "status": 409, "approval": dialog}
    try:
        want = asks.normalise(qs, answers)
    except asks.Refused as e:
        return False, {"error": str(e), "status": e.status, "approval": dialog}
    tabbed = not (len(qs) == 1 and not qs[0]["multiSelect"])
    try:
        asks.drive(asks.Screen(pane, sessions._capture_pane), qs, want, tabbed=tabbed)
    except asks.Stuck as e:
        now = sessions.approval_for(pane, agent, session)
        log.warning("answer: %s stuck on %s", session[:8], e)
        return False, {"error": f"the question did not take the answer: {e}", "status": 504,
                       "session": session, "pane": pane, "approval": now}
    deadline = time.monotonic() + 3.0
    while True:
        now = sessions.approval_for(pane, agent, session)
        if not now or now["key"] != dialog["key"] or now.get("kind") != "question":
            return True, {"session": session, "pane": pane,
                          "answers": asks.as_text(qs, want),
                          "waiting": bool(now), "approval": now}
        if time.monotonic() >= deadline:
            return False, {"error": "the question is still on screen", "status": 504,
                           "session": session, "pane": pane, "approval": now}
        time.sleep(0.3)


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

    `host` names the tmux session to open it in; by default the layout's
    (`layout.revive_host`): the one someone is attached to on David's desk,
    `sasonica` otherwise. `flags` go to `claude` (a fresh session may want
    `--dangerously-skip-permissions`; a revived one wants nothing).

    Background (`-d`) on purpose: this is triggered from the phone, and a
    window that steals the desk's focus mid-task is a worse answer than one
    that waits to be found. The app is handed the pane id so it can offer a
    link back to it.

    Two things observed doing this for real, neither a fault:

    * On David's desk (`layout.expects_move_hook`) the window does not stay
      where we put it — a SessionStart hook moves it into the session named
      for its project. The pane id survives the move, so
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
    if not host:
        # David's desk: "" — the session someone is attached to. The default
        # layout: `sasonica`, with a client held on it.
        from agent_media_core import layout

        host = layout.revive_host()
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

    The layout's answer (agent_media_core.layout.fresh_target). On David's
    desk it is copied from the amux registration named by MEDIA_ASK_SESSION
    (default `scratch`) — the same directory and flags `amux start scratch`
    would use, in the tmux session amux would put it in — so where a
    phone-started chat lands is set in one place, with amux. In the default
    layout it is home, in the one `sasonica` session. Each part can be
    overridden (MEDIA_ASK_TMUX / MEDIA_ASK_CWD / MEDIA_ASK_FLAGS).
    """
    from agent_media_core import layout

    host, cwd, flags = layout.fresh_target()
    return host, cwd, shlex.split(flags)


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

#: What Claude Code shows in the composer in place of a pasted message.
_PASTE_PLACEHOLDER = "[Pasted text"


def _unsent(pane: str, head: str | tuple[str, ...], agent: str, window: float) -> bool:
    """Whether the message is still sitting in `pane`'s composer for all of `window`.

    `head` is one mark or several (the message's first and last words): the
    message counts as still there if ANY of them is. One mark was not enough —
    a long message in a phone-width pane wraps, the composer scrolls to its
    end, the first words leave the screen, and "the head is gone" read as
    "sent" while the whole message sat in the box (2026-09-22, a new chat
    from the app that never went out).

    False the moment the box lets go of it — that is the line being taken, and
    it is the signal worth trusting: `_classify_agent` needs the footer, which
    a narrow pane truncates away, while an empty composer is an empty composer
    at any width.

    Fails open. A capture that comes back empty reads as taken, because the
    alternative is hammering Enter at a pane we cannot see.
    """
    marks = (head,) if isinstance(head, str) else tuple(m for m in head if m)
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
            if not any(m in " ".join(" ".join(box).split()) for m in marks):
                return False
        elif agent == "opencode":
            # opencode's composer is the run of `┃` lines straight above the
            # `╹▀▀▀` that closes it; a sent message is drawn with the same
            # bar further up, but with the model's line between.
            lines = cap.splitlines()
            end = next((i for i in range(len(lines) - 1, -1, -1)
                        if lines[i].lstrip().startswith("╹")), -1)
            box = []
            for ln in reversed(lines[:end] if end >= 0 else []):
                if not ln.lstrip().startswith("┃"):
                    break
                box.append(ln.lstrip()[1:])
            if not any(m in " ".join(" ".join(box).split()) for m in marks):
                return False
        else:
            flat = " ".join(cap.split())
            i = flat.rfind(_COMPOSER.get(agent, sessions._PROMPT_GLYPH))
            if i < 0:
                return False
            # A message Claude Code took as a paste sits in the box as a
            # placeholder ("[Pasted text #1 +12 lines]"), none of its words
            # on screen: still unsent while the placeholder is there.
            if not any(m in flat[i:] for m in (*marks, _PASTE_PLACEHOLDER)):
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

    flat = " ".join(text.split())
    # First and last words: whichever end of a wrapped message is on screen.
    marks = (flat[:24], flat[-24:]) if len(flat) > 24 else (flat,)
    return conv.submit(pane, lambda window: not _unsent(pane, marks, agent, window),
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



def _agent_unready(agent: str) -> str:
    """Why a fresh `agent` chat would not answer, or "" if it should.

    Only the two that can be asked are asked (`harnesses.auth_state`): pi and
    Hermes answer "unknown" without a terminal, and a guess there would block
    a chat that works. A resumed session is not checked — it is already
    running, whatever the credentials on disk now say.
    """
    from agent_media_core import harnesses

    if not harnesses.program(agent):
        return f"{agent} is not installed on this host"
    try:
        state, _who = harnesses.auth_state(agent, timeout=5.0)
    except Exception:          # noqa: BLE001 - a check that fails is not a refusal
        return ""
    return f"{agent} is signed out on this host" if state == "out" else ""


def ask(text: str, bearer: str, *, quote: str = "", project: str = "",
        agent: str = "", cwd: str = "", cwd_trusted: bool = False) -> tuple[bool, dict]:
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
    `cwd_trusted` is for the server's own callers (a chat about a note opens
    in the notes tree), never for a directory the phone named.
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
    why = _agent_unready(agent)
    if why:
        # Without this the window opens and the harness sits on its own
        # sign-in screen: a thread that appears in the app, is typed into,
        # and never answers. Saying so here is the difference between a
        # visible error and a conversation that looks stuck.
        return False, {"error": why, "status": 409, "agent": agent,
                       "fix": "harnesses"}
    where = (cwd or "").strip()
    host, cwd, flags = ask_target()
    if agent != "claude":
        flags = []          # amux's flags are claude's (--dangerously-skip-permissions)
    if where:
        # A directory named outright. Only somewhere a session has actually
        # run: this opens a shell there, so it is not the phone's to choose
        # freely.
        if not cwd_trusted and where not in {p["path"] for p in sessions.places(limit=0)}:
            return False, {"error": f"no session has run in {where!r}", "status": 404}
        from agent_media_core import layout

        host, cwd = layout.place_host(where), where
    elif project:
        host, cwd = sessions.project_target(project)
        if not cwd:
            return False, {"error": f"no directory known for project {project!r}", "status": 404}
    # A pane, or (MEDIA_HEADLESS) a headless process sessiond holds — see
    # driver/. The directory and the tmux session name were chosen above,
    # the same for both: the name is where a pane would open, and what a
    # headless session is filed and voiced under.
    from agent_media_core import layout

    from . import driver

    chosen = driver.for_new(agent)
    if chosen.kind == driver.HEADLESS:
        # No pane, so no tmux session to be filed under: the layout says what
        # it would have been (David's: the session itself; default: the folder).
        host = layout.workspace_for(host, cwd)
    return chosen.start(agent=agent, cwd=cwd, text=text, host=host,
                        flags=flags, quote=quote)


def _ask_pane(text: str, *, agent: str, cwd: str, host: str, flags: list[str],
              quote: str = "") -> tuple[bool, dict]:
    """The pane driver's fresh session: a background tmux window in `host`,
    the words typed in, the listener's turn shelved against its uuid."""
    # pi takes its id up front; the others are asked for theirs.
    fixed = str(uuid.uuid4()) if agent == "pi" else ""
    pane, err = open_window(fixed, cwd, resume=False, host=host, flags=flags, agent=agent)
    if err:
        return False, {"error": err, "pane": pane or None}
    # A codex has no session until its first message is in, so it is asked
    # after the send.
    session = fixed or (sessions.session_of_pane(pane) if agent == "claude" else "")
    _settle(pane)
    body = for_pane(compose(text, quote), pane, agent)
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
    from . import driver

    return driver.for_session(session).resume(session)


def _resume_pane(session: str) -> tuple[bool, dict]:
    """The pane driver's resume: already live says where; else a background
    window running `claude --resume` (or the harness's own)."""
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
    from . import rest

    rest.clear_quietly(session)          # resumed: no longer resting
    return True, {"session": session, "pane": pane, "live": True, "opened": True}


def close_pane(session: str, pane: str = "") -> tuple[bool, dict]:
    """Close the pane `session` runs in. No gate: the callers are the route
    below, which has checked the bearer, and the idle reaper, which runs as
    the host's own user.

    Only a pane that hosts this very session is touched — never a shell, and
    never a pane that has since been recycled for something else. `pane` is
    what the caller already found; it is used only if a fresh sweep still
    finds the session there.
    """
    live = sessions.live_sessions().get(session, "")
    if not live or (pane and live != pane):
        return True, {"session": session, "live": False, "closed": False}
    pane = live
    if not panes._tmux(["kill-pane", "-t", pane]) and panes._tmux(["display", "-pt", pane, "#{pane_id}"]):
        return False, {"error": f"could not close {pane}", "pane": pane}
    _retag(session)
    return True, {"session": session, "pane": pane, "live": False, "closed": True}


def session_close(session: str, bearer: str) -> tuple[bool, dict]:
    """End a conversation's session: close the pane it runs in.

    Claude Code ends the session cleanly on the pane closing (SessionEnd
    fires), and the transcript stays, so this is undone by `session_resume`.
    Ended by a person, so never marked rested (rest.py); a mark left from an
    earlier reaper close is dropped, since the person has now decided.
    """
    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    from . import driver

    ok, detail = driver.for_session(session).close(session)
    if ok and detail.get("closed"):
        from . import rest

        rest.clear_quietly(session)
    return ok, detail


# --- the whole move -----------------------------------------------------------

# A quoted turn is context, not a document: it goes in on one line, cut short.
_QUOTE_LIMIT = 160


def compose(text: str, quote: str = "") -> str:
    """What a pane is typed: the reply with its line breaks (trailing spaces
    and runs of blank lines tidied), after the quote on one line.

    The breaks survive to the pane only where it can take them
    (`panes.multiline_ok`, Claude Code in tmux: Alt+Enter); `for_pane`
    flattens for the rest, where a newline mid-message would submit the first
    half and leave the rest sitting in the composer.
    """
    lines = [ln.rstrip() for ln in (text or "").replace("\r\n", "\n").split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    quote = panes.flatten(quote)
    if not quote:
        return text
    if len(quote) > _QUOTE_LIMIT:
        quote = quote[:_QUOTE_LIMIT - 1] + "…"
    return f'Re: "{quote}" — {text}'


def for_pane(body: str, pane: str, agent: str = "claude") -> str:
    """`body` as `pane` can take it: flattened unless it can be given a newline."""
    return body if panes.multiline_ok(pane, agent) else panes.flatten(body)


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
    # The listener spoke again: a stop's cutoff on this session's speech ends
    # here (server-contract.md §12). Synchronous, so the reply this turn
    # starts cannot race it; the prompt hook ends it too, for the desk.
    try:
        from agent_media_core.intake.submit import end_session_speech_cut

        end_session_speech_cut(session)
    except Exception as e:  # noqa: BLE001 — the words reached the session
        print(f"reply: could not end the speech cutoff ({e})", file=sys.stderr)
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
          mode: str = "continue", session: str = "",
          keep_reading: bool = False) -> tuple[bool, dict]:
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

    A reply means the thread's last reply was read: its speech ends at the
    close of the sentence playing (`session_reply_read`), unless the box's
    chip was switched to Keep reading. Marked before the words go in, so the
    prompt hook they set off finds the choice already made.
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
    from . import driver

    if session:
        # A session id nothing knows — not running, no transcript — is the
        # same answer an item with no session behind it gets: not there.
        # Checked here rather than left to `deliver`, because `branch` would
        # otherwise open a fresh session in no particular directory.
        if not sessions.live_sessions().get(session) and not sessions.session_exists(session) \
                and not driver.owned_headless(session):
            return False, {"error": f"no such session {session[:8]}", "status": 404}
    else:
        session, err = sessions.session_for_item(item, bearer)
        if not session:
            return False, {"error": err, "status": 404}
    # `body` is what a pane is typed (its breaks kept only where the pane can
    # take them, `for_pane`); `text` is recorded with the breaks the reply box
    # had, so the transcript keeps them.
    body = compose(text, quote)
    if mode != "branch":
        from . import speech

        speech.reply_read(session, keep=keep_reading)

    if mode == "branch" and driver.owned_headless(session):
        # A branch runs where the thread it came from ran (proposal §8): a
        # fresh headless session in the same directory, seeded the same way.
        ok, detail = driver.headless_driver().start(
            agent="claude", cwd=sessions.transcript_cwd(session) or driver.headless_driver().cwd_of(session),
            text=text, quote=quote)
        if ok:
            detail["branched"] = True
        return ok, detail
    if mode == "branch":
        agent = sessions.agent_of(session)
        pane, err = open_window("", sessions.transcript_cwd(session), resume=False, agent=agent)
        if err:
            return False, {"error": err, "pane": pane or None}
        body = for_pane(body, pane, agent)
        send_err = _send_to_pane(pane, body)
        if send_err:
            return False, {"session": session, "pane": pane, "opened": True,
                           "branched": True, "error": send_err}
        if not _ensure_submitted(pane, body, agent=agent):
            return False, {**_unsent_error(pane, session), "branched": True}
        _record_turn(session, text, pane)
        return True, {"session": session, "pane": pane, "opened": True,
                      "branched": True, "submitted": True}

    return deliver(session, body, text, quote=quote)


def deliver(session: str, body: str, text: str, *, quote: str = "") -> tuple[bool, dict]:
    """Put `body` into `session`, reviving it if it has ended — through the
    driver that owns it (driver/).

    `text` is the listener's own words, shelved as their turn; `body` is what
    a pane is typed (the quote rides along in it, on one line). A headless
    session takes `text` as written and `quote` as its own paragraph. Shared
    by a reply from a conversation's page and a reply the assistant button
    routed here.
    """
    from . import driver

    return driver.for_session(session).send(session, body, text, quote=quote)


def _deliver_pane(session: str, body: str, text: str) -> tuple[bool, dict]:
    """The pane driver's send: type `body` into the live pane, reviving the
    session in a background window if it has ended."""
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
    body = for_pane(body, pane, agent)
    send_err = _send_to_pane(pane, body)
    if send_err:
        return False, {"error": send_err, "session": session, "pane": pane}
    # A long reply's Enter can arrive while the TUI is still taking the text
    # and be lost — the words sat in the box of a live session, unsent, until
    # someone pressed Enter by hand. Look, press, and look again.
    if not _ensure_submitted(pane, body, agent=agent):
        return False, {**_unsent_error(pane, session), "opened": opened}
    _record_turn(session, text, pane)
    # A thread you are talking to is not archived. Here rather than in
    # `reply`, so a routed `/ask` that lands in an archived thread clears it
    # too; only once the words are in, so a send that failed leaves it be.
    # Nor resting: a thread the reaper closed is in use again (rest.py).
    from . import archive, rest

    archive.unarchive_quietly(session)
    rest.clear_quietly(session)
    return True, {"session": session, "pane": pane, "opened": opened,
                  "submitted": True}
