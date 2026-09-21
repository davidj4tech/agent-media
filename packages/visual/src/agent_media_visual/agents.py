"""Getting an agent onto this host, and signing into it, from the phone.

The app can already start a conversation with any of the four harnesses — as
long as the harness is there and logged in. That last mile was the one thing
still only doable at the desk, and it is the one thing you notice from an
armchair: a new machine, or a token that expired overnight, and "new codex
chat" answers `codex: not found`.

`harnesses.RECIPES` is the table of what to run; this module is the doing of
it. Both halves work the same way, because both are terminal programs that
ask questions:

* the command is opened in a background tmux window on this host, the way a
  phone-started chat is (`send.open_window`), so what happens is visible at
  the desk and survives the app being closed;
* the phone reads that window's screen through `screen()` and types into it
  through `keys()` — an OAuth code pasted back, a `y` to a prompt;
* when the command exits, the window prints `[finished: <code>]` and the pane
  goes to sleep. It does NOT drop to a shell: keys from the phone reach the
  sign-in flow while it is running and nothing at all afterwards, so this
  endpoint is never a remote terminal. `close()` ends the window.

Only panes this module opened can be read or typed into, which is what keeps
`keys()` from being a way to go rummaging through the desk's other windows.
The gate on every call is the same ABS bearer the rest of the app carries
(`auth_abs.may_control_speech`) — installing an agent is no more authority than
`/ask` already hands out, which opens a session with permissions skipped.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from pathlib import Path

from agent_media_core import harnesses

from agent_media_server import auth_abs, panes, send, sessions

#: Named keys the phone may press. Text is typed literally; anything that is
#: not a plain line of text has to be one of these, so a request cannot ask
#: tmux for arbitrary key syntax.
KEYS = ("Enter", "Escape", "Space", "Tab", "BSpace", "Up", "Down", "Left", "Right",
        "C-c", "C-d")

#: What the window prints after the command, so the phone can stop polling.
DONE = re.compile(r"\[finished: (-?\d+)\]")

#: How long a finished window hangs around to be read before tmux reclaims it.
LINGER_S = 86400

ACTIONS = ("install", "login")


def _registry() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(os.path.expanduser(
        os.environ.get("MEDIA_AGENT_SETUP_DIR") or str(Path(base) / "agent-media" / "agent-setup")))


def _remember(pane: str, agent: str, action: str, cmd: str) -> None:
    d = _registry()
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / pane.lstrip("%")).write_text(json.dumps(
            {"pane": pane, "agent": agent, "action": action, "cmd": cmd, "at": time.time()}))
    except OSError:
        pass


def _row(pane: str) -> dict:
    """What we know about a pane we opened, or {} — the whole of the gate.

    A pane id is reused, so the row is believed only while tmux still has a
    pane by that name; a stale row names a window that is gone, and the
    caller gets the same "no such window" as a made-up one.
    """
    if not re.fullmatch(r"%[0-9]+", pane or ""):
        return {}
    try:
        row = json.loads((_registry() / pane.lstrip("%")).read_text())
    except (OSError, ValueError):
        return {}
    return row if isinstance(row, dict) else {}


def _alive(pane: str) -> bool:
    return bool(panes._tmux(["display", "-pt", pane, "#{pane_id}"]))


def _forget(pane: str) -> None:
    try:
        (_registry() / pane.lstrip("%")).unlink()
    except OSError:
        pass


# --- what is here ----------------------------------------------------------------

def agents(bearer: str) -> tuple[bool, dict]:
    """`/agents`: the four harnesses, and what each of them needs.

    `present` and `version` say whether a chat can be started with it at all;
    `auth` is "in", "out" or "unknown" — unknown being the honest answer for
    pi and Hermes, neither of which will say without a terminal. `actions`
    are the buttons worth showing: what this host actually has a recipe for.
    """
    ok, detail = auth_abs.may_control_speech(bearer)
    if not ok:
        return False, detail
    rows = []
    for name in harnesses.HARNESSES:
        here = harnesses.program(name)
        state, who = harnesses.auth_state(name) if here else ("unknown", "")
        actions = []
        if harnesses.install_argv(name):
            actions.append("install")
        if here and harnesses.login_argv(name):
            actions.append("login")
        rows.append({
            "name": name,
            "present": bool(here),
            "path": here,
            "version": harnesses.version_of(name) if here else "",
            "auth": state,
            "account": who,
            "actions": actions,
            # An install of something already here is an update, and the
            # button should say so rather than pretending otherwise.
            "installed_action": "update" if here else "install",
        })
    return True, {"agents": rows}


# --- doing it --------------------------------------------------------------------

def _window(argv: list[str], title: str) -> tuple[str, str]:
    """Open a background window running `argv`. `(pane, error)`.

    The command is wrapped so that the window outlives it: an installer that
    fails says why on the last line, and that line is the whole point of
    watching from the phone. After it, `sleep` — not a shell — holds the pane
    open (see the module docstring).
    """
    host, cwd, _flags = send.ask_target()
    if not send.ensure_host(host, cwd):
        return "", f"could not get a client onto tmux session {host!r}"
    inner = (f"{shlex.join(argv)}; printf '\\n[finished: %s]\\n' \"$?\"; "
             f"exec sleep {LINGER_S}")
    # `harnesses.bin_path` in the window too: npm is as missing from a
    # systemd PATH as claude was, and this command is usually npm.
    cmd = (f"exec env PATH={shlex.quote(harnesses.bin_path())} "
           f"sh -c {shlex.quote(inner)}")
    pane = panes._tmux(["new-window", "-d", "-t", f"{host}:", "-c", cwd,
                        "-n", title, "-P", "-F", "#{pane_id}", cmd])
    return (pane, "") if pane else ("", "tmux could not open a window")


def run(agent: str, action: str, bearer: str) -> tuple[bool, dict]:
    """`/agents/run`: install (or update) an agent, or sign into it.

    Answers with the pane it opened and the command it is running; the phone
    then polls `screen()` for the same window the desk can see.
    """
    ok, detail = auth_abs.may_control_speech(bearer)
    if not ok:
        return False, detail
    agent = (agent or "").strip()
    action = (action or "").strip()
    if agent not in harnesses.HARNESSES:
        return False, {"error": f"not an agent: {agent!r}", "status": 400}
    if action not in ACTIONS:
        return False, {"error": f"not an action: {action!r}", "status": 400}
    argv = (harnesses.install_argv(agent) if action == "install"
            else harnesses.login_argv(agent))
    if not argv:
        why = (f"no install recipe for {agent}" if action == "install"
               else f"{agent} has no sign-in to run"
               + ("" if harnesses.installed(agent) else f" ({agent} is not installed)"))
        return False, {"error": why, "status": 409}
    pane, err = _window(argv, f"{agent}-{action}")
    if err:
        return False, {"error": err, "status": 503}
    cmd = shlex.join(argv)
    _remember(pane, agent, action, cmd)
    return True, {"pane": pane, "agent": agent, "action": action, "cmd": cmd}


def screen(pane: str, bearer: str, lines: int = 60) -> tuple[bool, dict]:
    """`/agents/screen`: what that window is showing, and whether it is over.

    `done` comes from the marker the wrapper prints, not from the process
    being gone: the pane deliberately stays alive afterwards so the last
    screen can still be read.
    """
    ok, detail = auth_abs.may_control_speech(bearer)
    if not ok:
        return False, detail
    row = _row(pane)
    if not row:
        return False, {"error": f"not a setup window: {pane!r}", "status": 404}
    if not _alive(pane):
        _forget(pane)
        return False, {"error": f"window {pane} is gone", "status": 410}
    text = panes.strip_ansi(sessions._capture_pane(pane))
    found = DONE.search(text)
    rows = [ln.rstrip() for ln in text.splitlines()]
    while rows and not rows[-1]:
        rows.pop()
    return True, {"pane": pane, "agent": row.get("agent", ""),
                  "action": row.get("action", ""), "cmd": row.get("cmd", ""),
                  "lines": rows[-lines:] if lines else rows,
                  "done": bool(found),
                  "exit": int(found.group(1)) if found else None}


def keys(pane: str, text: str, key: str, bearer: str) -> tuple[bool, dict]:
    """`/agents/keys`: type into that window — a pasted code, a y, an Enter.

    Text goes in literally and is sent as typed; `key` presses one of `KEYS`
    instead. Both refuse any pane this module did not open.
    """
    ok, detail = auth_abs.may_control_speech(bearer)
    if not ok:
        return False, detail
    if not _row(pane):
        return False, {"error": f"not a setup window: {pane!r}", "status": 404}
    if not _alive(pane):
        _forget(pane)
        return False, {"error": f"window {pane} is gone", "status": 410}
    key = (key or "").strip()
    if key and key not in KEYS:
        return False, {"error": f"not a key we press: {key!r}", "status": 400}
    text = (text or "").strip()
    if not text and not key:
        return False, {"error": "nothing to type", "status": 400}
    if text:
        # A code pasted from a browser is one line; a newline in it would
        # submit halfway through, so only the first line is typed.
        panes._tmux(["send-keys", "-t", pane, "-l", text.splitlines()[0]])
    if key:
        panes._tmux(["send-keys", "-t", pane, key])
    return True, {"pane": pane}


def close(pane: str, bearer: str) -> tuple[bool, dict]:
    """`/agents/close`: end that window. Ours only, and forgotten afterwards."""
    ok, detail = auth_abs.may_control_speech(bearer)
    if not ok:
        return False, detail
    if not _row(pane):
        return False, {"error": f"not a setup window: {pane!r}", "status": 404}
    if _alive(pane):
        panes._tmux(["kill-pane", "-t", pane])
    _forget(pane)
    return True, {"pane": pane}
