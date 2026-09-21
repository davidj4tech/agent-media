"""Where an agent runs, and how to reach it.

A session is addressed by the pane it lives in, and until now that pane was
always tmux's: a `%562` handed straight to `send-keys` and `capture-pane`. A
herdr pane is just as reachable — `herdr pane read` captures it with colour,
`herdr pane send-text` types into it — so an address carries its multiplexer
and the three verbs a destination needs dispatch on it:

    "%562"          a tmux pane, as tmux names it
    "herdr:w6:p1"   a herdr pane, as herdr names it

tmux keeps the bare form on purpose. Pane ids are written into speech history,
conversation manifests and the popup's own state, and every one of those rows
was written before herdr existed; a prefix here would orphan all of them.

Discovery is the same shape for both: a live agent process inherits
`TMUX_PANE` or `HERDR_PANE_ID` from the pane that started it, so `addr_of_env`
reads a process's address straight out of `/proc/<pid>/environ`.
"""

from __future__ import annotations

import subprocess
import time

HERDR = "herdr:"


def is_herdr(addr: str) -> bool:
    return (addr or "").startswith(HERDR)


def herdr_pane(addr: str) -> str:
    """The pane id herdr knows, with our prefix off."""
    return addr[len(HERDR):] if is_herdr(addr) else addr


def addr_of_env(env: dict[bytes, bytes]) -> str:
    """The address of the pane a process is running in, or "".

    tmux first: a herdr pane that itself holds a tmux client would carry both,
    and the inner multiplexer is the one that can type into the agent.
    """
    pane = env.get(b"TMUX_PANE", b"").decode(errors="replace")
    if pane:
        return pane
    herdr = env.get(b"HERDR_PANE_ID", b"").decode(errors="replace")
    return f"{HERDR}{herdr}" if herdr else ""


def _run(argv: list[str], timeout: int = 10) -> str:
    try:
        out = subprocess.run(argv, capture_output=True, text=True,
                             timeout=timeout, check=False)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# --- the three verbs ----------------------------------------------------------


def alive(addr: str) -> bool:
    """Whether that pane still exists."""
    if not addr:
        return False
    if is_herdr(addr):
        return bool(_run(["herdr", "pane", "get", herdr_pane(addr)]))
    return bool(_run(["tmux", "display-message", "-pt", addr, "#{pane_id}"]))


def capture(addr: str, lines: int = 40, ansi: bool = True) -> str:
    """The bottom `lines` rows of that pane, with colour unless asked otherwise.

    Colour is not decoration here: the ghost prompt is found by its dim run,
    and herdr emits `ESC[2m` for it exactly as tmux does.
    """
    if not addr:
        return ""
    if is_herdr(addr):
        argv = ["herdr", "pane", "read", herdr_pane(addr),
                "--source", "recent", "--lines", str(lines),
                "--format", "ansi" if ansi else "text"]
        return _run(argv)
    argv = ["tmux", "capture-pane", "-t", addr, "-p", "-S", f"-{lines}"]
    if ansi:
        argv.insert(-2, "-e")
    return _run(argv)


def send(addr: str, text: str) -> str:
    """Type `text` then Enter into that pane. Returns "" or the error.

    Literal-then-Enter, with a beat between: Claude Code's input buffering
    drops an Enter that arrives in the same breath as the text.
    """
    if not alive(addr):
        return f"pane {addr} is gone"
    try:
        if is_herdr(addr):
            pane = herdr_pane(addr)
            subprocess.run(["herdr", "pane", "send-text", pane, text],
                           capture_output=True, timeout=5, check=True)
            time.sleep(0.05)
            subprocess.run(["herdr", "pane", "send-keys", pane, "enter"],
                           capture_output=True, timeout=5, check=True)
            return ""
        subprocess.run(["tmux", "send-keys", "-t", addr, "-l", text],
                       timeout=5, check=True)
        time.sleep(0.05)
        subprocess.run(["tmux", "send-keys", "-t", addr, "Enter"],
                       timeout=5, check=True)
        return ""
    except (OSError, subprocess.SubprocessError) as e:
        return f"send: {e}"


def focus(addr: str) -> bool:
    """Bring that pane up on whatever is attached to it."""
    if is_herdr(addr):
        return bool(_run(["herdr", "pane", "focus", herdr_pane(addr)]))
    return bool(_run(["tmux", "switch-client", "-t", addr]) or
                _run(["tmux", "select-pane", "-t", addr]))


# --- what the pane is called --------------------------------------------------


def info(addr: str) -> dict:
    """herdr's own record of a pane: agent, agent_status, cwd, terminal title.

    One call answers what tmux needs three `display-message`s for, so the
    readers below share it rather than each asking again.
    """
    if not is_herdr(addr):
        return {}
    import json

    out = _run(["herdr", "pane", "get", herdr_pane(addr)])
    try:
        return (json.loads(out).get("result") or {}).get("pane") or {}
    except (ValueError, AttributeError):
        return {}


def label(addr: str) -> str:
    """The pane's own name — or "" when it has none.

    Claude Code writes its conversation's title into the TERMINAL title, which
    is what makes this worth asking for at all. herdr reports that stripped of
    its spinner as `terminal_title_stripped`, and a pane the person has named
    themselves carries a `label` on top of it, which wins.
    """
    if not addr:
        return ""
    if is_herdr(addr):
        pane = info(addr)
        return str(pane.get("label") or pane.get("terminal_title_stripped") or "").strip()
    return _run(["tmux", "display-message", "-pt", addr, "#{pane_title}"])


def cwd(addr: str) -> str:
    """The directory that pane is working in."""
    if not addr:
        return ""
    if is_herdr(addr):
        pane = info(addr)
        return str(pane.get("foreground_cwd") or pane.get("cwd") or "")
    return _run(["tmux", "display-message", "-pt", addr, "#{pane_current_path}"])


def process_name(addr: str) -> str:
    """The name of the program in the foreground of that pane ("claude"), or ""."""
    if not is_herdr(addr):
        return _run(["tmux", "display", "-pt", addr, "#{pane_current_command}"])
    import json

    # herdr detects the agent itself and says so on the pane; that is the same
    # answer as tmux's current command, without a second process walk.
    named = str(info(addr).get("agent") or "").strip()
    if named:
        return named
    out = _run(["herdr", "pane", "process-info", "--pane", herdr_pane(addr)])
    try:
        proc = (json.loads(out).get("result") or {}).get("process_info") or {}
        procs = proc.get("foreground_processes") or []
    except (ValueError, AttributeError):
        return ""
    # The agent is the deepest foreground process; a shell is what is left
    # when nothing is running in it.
    for proc in reversed(procs):
        name = str(proc.get("name") or "")
        if name and name not in ("zsh", "bash", "sh", "fish"):
            return name
    return str(procs[0].get("name") or "") if procs else ""


def where(addr: str) -> dict:
    """`{source, session}` — which multiplexer holds the pane, and its grouping
    (a tmux session name, a herdr workspace id). Empty values when unknown."""
    if is_herdr(addr):
        return {"source": "herdr", "session": herdr_pane(addr).split(":")[0]}
    return {"source": "tmux",
            "session": _run(["tmux", "display", "-pt", addr, "#{session_name}"])}
