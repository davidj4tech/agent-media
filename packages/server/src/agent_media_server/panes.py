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

Reading a pane is here too: what state the agent in it is in (`classify`),
and which panes hold an agent at all (`tmux_agent_panes`,
`herdr_agent_panes`). These began in the canvas, which still answers to its
old names for them (`canvas._classify_agent` and friends).
"""

from __future__ import annotations

import re
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


#: The most typed at once. Claude Code reads a burst of more than ~900
#: characters arriving together as a paste (measured 22 Sep 2026: 800 typed,
#: 1000 a "[Pasted text #1]" placeholder), and a paste reaches the model
#: wrapped in `<pasted_content>` — as material, not as the listener's
#: instruction, which the model then declines to act on. Each `send-keys` is
#: its own write, so a long message goes in as several.
TYPE_CHUNK = 400


def flatten(text: str) -> str:
    """`text` on one line: for a composer that cannot be given a newline."""
    return " ".join((text or "").split())


def multiline_ok(addr: str, agent: str = "claude") -> bool:
    """Whether a message with line breaks can go into this pane as written.

    Claude Code in tmux: yes, a newline is Alt+Enter (`M-Enter`). Not
    bracketed paste, though tmux can do it (`paste-buffer -p`): Claude Code
    wraps every paste, even one line, in `<pasted_content>`, and the model
    treats it as quoted material rather than as what it was asked (probed 22
    Sep 2026, v2.1.278). The others are unprobed — Codex, pi, Hermes — and so
    is herdr, which has no key-by-key send to put Alt+Enter through: flattened.
    """
    return agent == "claude" and not is_herdr(addr)


def _type_tmux(addr: str, text: str) -> None:
    """Type `text` into a tmux pane, a line at a time with Alt+Enter between
    (Claude Code's newline), each line in `TYPE_CHUNK` pieces."""
    for n, line in enumerate(text.split("\n")):
        if n:
            subprocess.run(["tmux", "send-keys", "-t", addr, "M-Enter"],
                           timeout=5, check=True)
        for k in range(0, len(line), TYPE_CHUNK):
            subprocess.run(["tmux", "send-keys", "-t", addr, "-l", line[k:k + TYPE_CHUNK]],
                           timeout=5, check=True)


def send(addr: str, text: str) -> str:
    """Type `text` then Enter into that pane. Returns "" or the error.

    Literal-then-Enter, with a beat between: Claude Code's input buffering
    drops an Enter that arrives in the same breath as the text. A line break
    in `text` is typed as Alt+Enter, which only Claude Code reads as a newline
    — callers flatten for the rest (`multiline_ok`). herdr is always flattened.
    """
    if not alive(addr):
        return f"pane {addr} is gone"
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    try:
        if is_herdr(addr):
            pane = herdr_pane(addr)
            subprocess.run(["herdr", "pane", "send-text", pane, flatten(text)],
                           capture_output=True, timeout=5, check=True)
            time.sleep(0.05)
            subprocess.run(["herdr", "pane", "send-keys", pane, "enter"],
                           capture_output=True, timeout=5, check=True)
            return ""
        _type_tmux(addr, text)
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


# --- what the agent in a pane is doing -------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def _stdout(argv: list[str], timeout: int = 10) -> str:
    """stdout of `argv`, stripped; "" on any failure or timeout.

    Unlike `_run`, a non-zero exit still answers with whatever was printed —
    the canvas's runner, which the pane sweep below was written against.
    """
    try:
        out = subprocess.run(argv, capture_output=True, text=True,
                             timeout=timeout)
        return (out.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


#: An option line in any agent's dialog: "❯ 1. Yes", "  2. No", "› 3. …".
_NUMBERED = re.compile(r"^\s*[❯›>↓↑]?\s*\d+\.\s+\S", re.M)
#: The one that is selected. A reply full of numbered points is not a dialog;
#: a dialog shows which option the arrow keys are on — unless the list is
#: long enough to scroll, and the marked one is off the top.
_SELECTED = re.compile(r"^\s*[❯›>]\s*\d+\.\s+\S", re.M)
#: So the other tell is the dialog's own instructions, which every one of
#: them prints under the list.
_KEYS = re.compile(r"Enter to select|↑/↓ to navigate|Press enter to confirm"
                   r"|Esc to cancel|esc to cancel")


def classify_cc(pane: str) -> "str | None":
    """Classify an ANSI-stripped capture of a Claude Code TUI → working / input
    / approval, or None if it doesn't look like Claude Code (so plain shells,
    vim, etc. are ignored). Mirrors amux's detector: require CC chrome, check a
    permission dialog BEFORE the working signal (CC shows "esc to interrupt"
    even while a dialog blocks), and match the width-truncated "esc…" too."""
    # A dialog is checked before the chrome, because it covers the chrome: an
    # open permission prompt hides the footer this would otherwise recognise,
    # so a session stopped on one did not look like Claude Code at all. Two
    # numbered options at least — one line the person typed themselves ("❯ 1.
    # do the thing") is not a dialog anybody may answer.
    if len(_NUMBERED.findall(pane)) >= 2 and (_SELECTED.search(pane) or _KEYS.search(pane)):
        return "approval"
    # The footer names the permission mode, and on a phone-width pane that is
    # often all of it that fits: "⏸ plan mode on (shift+tab to…".
    if not re.search(r"\? for shortcuts|bypass permissions|esc to interrupt|"
                     r"esc…|⏵⏵|[⏸⏵] \w+ mode on|\(shift\+tab", pane):
        return None
    if re.search(r"Do you want to |Yes, (and|allow|proceed)", pane):
        return "approval"
    # A phone-width pane cuts the footer's "esc to interrupt" down to "· e…",
    # and a session hard at work then read as one waiting on you — green on
    # the phone's shelf. The spinner line ("✽ Gitifying… (thought for 7s)")
    # says the same thing and survives any width.
    if re.search(r"· [↑↓] [0-9.]+k? tokens|esc to interrupt|·\s*es?c?…|\(thought for ", pane):
        return "working"
    return "input"


#: What each coding agent's pane reports as its command. Codex and pi hold
#: conversations the phone can reach too (see agent_media_core.harnesses).
AGENT_COMMANDS = ("claude", "codex", "pi", "hermes")


def classify(pane: str, agent: str = "claude") -> "str | None":
    """`classify_cc` for any agent: working / input / approval, or None when
    the capture does not look like that agent's TUI (not painted yet).

    Codex marks a turn with "esc to interrupt" as Claude does, its composer
    with `›`, and asks before a command with "Yes, proceed"; pi has a
    "Working..." spinner, an editor boxed by two rules, and no approvals.
    """
    if agent == "codex":
        # Changed hooks.json holds the whole TUI on a trust prompt until
        # someone at the desk answers it.
        if ((len(_NUMBERED.findall(pane)) >= 2 and (_SELECTED.search(pane) or _KEYS.search(pane)))
                or re.search(r"Would you like to (?:run|make|apply) |Yes, proceed|Hooks need review", pane)):
            return "approval"
        if re.search(r"esc to interrupt|esc…|Working \(", pane):
            return "working"
        if re.search(r"^\s*› ", pane, re.M):
            return "input"
        return None
    if agent == "hermes":
        # Its status line is the whole tell: "─ ready │ <model>" between turns,
        # a spinner and "formulating…" (or another verb) while it answers, and
        # the composer's placeholder says which of the two it is.
        if re.search(r"Ctrl\+C to interrupt|formulating…|thinking…|\bworking…", pane):
            return "working"
        if re.search(r"─ ready\s*│|❯ Ask me anything", pane):
            return "input"
        return None
    if agent == "pi":
        # The editor is a box of two full-width rules near the bottom; the
        # footer under it is cut short on a narrow pane, so it is no marker.
        tail = pane.rstrip("\n").splitlines()[-14:]
        if sum(1 for ln in tail if re.fullmatch(r"\s*─{8,}\s*", ln)) < 2:
            return None
        return "working" if re.search(r"Working\.\.\.", pane) else "input"
    return classify_cc(pane)


def agent_by_argv(pid: str) -> str:
    """The agent a pane is running when its command name does not say so.

    Hermes is the case: the process is the venv's python with the `hermes`
    script as its first argument, so `pane_current_command` is `python3`.
    """
    if not pid:
        return ""
    from agent_media_core import harnesses

    argv = harnesses._argv(pid)
    return harnesses.HERMES if harnesses._hermes_pid(argv) else ""


# --- which panes hold an agent ---------------------------------------------------


def tmux_agent_panes() -> list[dict]:
    """Auto-discover Claude Code across ALL tmux panes (not just each session's
    active one — a session can hold several agents in different windows),
    EXCLUDING amux's own `amux-*` sessions (those come from `amux ls`). One agent
    per CC pane, replyable by its pane id. Display name is the session, with the
    window appended when a session holds more than one CC pane."""
    out = _stdout(["tmux", "list-panes", "-a", "-F",
                   "#{pane_id}\t#{pane_current_command}\t#{session_name}\t"
                   "#{window_name}\t#{pane_current_path}\t#{pane_pid}"])
    panes_pids = {ln.split("\t")[0]: ln.split("\t")[5]
                  for ln in out.splitlines() if len(ln.split("\t")) >= 6}
    agents: list[dict] = []
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) < 5:
            continue
        pane_id, cmd, sess, win, cwd = f[:5]
        # Claude Code panes report `claude` as their command — a cheap, exact
        # filter (no need to capture shells/editors). Skip amux-managed ones.
        # Hermes is a console script, so its pane says `python3`: only those
        # are looked at more closely, by the argv of the pane's own process.
        if not pane_id or sess.startswith("amux-"):
            continue
        if cmd not in AGENT_COMMANDS:
            cmd = agent_by_argv(panes_pids.get(pane_id, ""))
            if not cmd:
                continue
        cap = strip_ansi(_stdout(["tmux", "capture-pane", "-t", pane_id,
                                  "-p", "-S", "-40"]))
        preview = next((ln.strip()[:60] for ln in reversed(cap.splitlines())
                        if ln.strip()), "")
        # A window named for the process ("python3", how Hermes shows up) is
        # no name for a conversation; the tmux session is the better one.
        if win == f[1]:
            win = ""
        agents.append({"name": (win if win and win != sess else sess),
                       "session": sess,
                       "state": classify(cap, cmd) or "input",
                       "agent": cmd,
                       "dir": cwd, "preview": preview,
                       "source": "tmux", "pane": pane_id})
    return agents


def herdr_agent_panes() -> list[dict]:
    """The same rows as `tmux_agent_panes`, for agents hosted by herdr.

    Discovery is the live-process walk `sessions.live_sessions` already does —
    herdr's own pane list says which panes exist, not which of them is an
    agent we may type into.
    """
    from . import sessions

    agents = []
    for addr in {a for a in sessions.live_sessions().values() if is_herdr(a)}:
        cap = strip_ansi(capture(addr, lines=40, ansi=False))
        agent = sessions._agent_of_pane(addr) or "claude"
        preview = next((ln.strip()[:60] for ln in reversed(cap.splitlines())
                        if ln.strip()), "")
        agents.append({"name": label(addr) or herdr_pane(addr),
                       "session": where(addr).get("session", ""),
                       "state": classify(cap, agent) or "input",
                       "agent": agent,
                       "dir": cwd(addr), "preview": preview,
                       "source": "herdr", "pane": addr})
    return agents


def _tmux(argv: list[str], timeout: int = 10) -> str:
    """`tmux <argv>`'s stdout, stripped; "" on failure or a non-zero exit."""
    try:
        out = subprocess.run(["tmux", *argv], capture_output=True, text=True,
                             timeout=timeout, check=False)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""
