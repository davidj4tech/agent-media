"""`!` in the reply box: a shell command, run where the session runs.

Claude Code's bash mode (a prompt that starts with `!`) runs the rest as a
command in the session's directory and puts the command and its output into
the conversation as two user records, `<bash-input>…</bash-input>` then
`<bash-stdout>…</bash-stdout><bash-stderr>…</bash-stderr>`; the agent then
answers them like any message (measured 29 Sep 2026: a tmux pane typed
`!pwd` by `send-keys -l`, Claude Code 2.1.x).

**A pane** gets the `!` typed as is: its own terminal does all of it.
**A headless session** (`claude -p`, stream-json) does not — the `!` reaches
the model as words and it runs the command with its Bash tool — so the server
runs it here, in the session's directory, and sends the same tagged text in,
one message. The thread reads either as one user message with a `shell` field
(transcript.py).

The command runs with the server's own rights, which are the session's: a
paired device could already ask the agent to run anything.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading

#: Claude Code's own limit for bash mode.
TIMEOUT_S = 120.0
#: Each stream, as sent to the session (Claude Code's BASH_MAX_OUTPUT_LENGTH).
OUTPUT_MAX = 30_000

_TAGS = re.compile(r"<(bash-input|bash-stdout|bash-stderr)>(.*?)</\1>", re.S)


def command_of(text: str) -> str:
    """The command in a reply that is one (`!` first), else ""."""
    text = (text or "").strip()
    if not text.startswith("!"):
        return ""
    return text[1:].strip()


def _cap(s: str) -> str:
    if len(s) <= OUTPUT_MAX:
        return s
    return s[:OUTPUT_MAX] + f"\n… ({len(s) - OUTPUT_MAX} more characters cut)"


def _shell() -> list[str]:
    """What runs a command: bash, as Claude Code's bash mode does — on Windows
    the one Git for Windows brings (Claude Code needs it there too), else
    `cmd /c`."""
    import shutil

    if os.name != "nt":
        return ["bash", "-c"]
    bash = shutil.which("bash")
    if not bash:
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            cand = os.path.join(root, "Git", "bin", "bash.exe") if root else ""
            if cand and os.path.exists(cand):
                bash = cand
                break
    return [bash, "-c"] if bash else ["cmd", "/c"]


def run(command: str, cwd: str, timeout: float = TIMEOUT_S) -> tuple[str, str]:
    """(stdout, stderr) of `command` in `cwd`, by bash, as Claude Code runs it.
    A timeout or a failure to start is said in stderr."""
    try:
        p = subprocess.run([*_shell(), command], cwd=cwd or None, capture_output=True,
                           text=True, errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL, env=os.environ.copy())
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode(errors="replace")
        return out.rstrip("\n"), f"Command timed out after {int(timeout)}s"
    except OSError as e:
        return "", f"Could not run the command: {e}"
    err = p.stderr.rstrip("\n")
    if p.returncode and not err:
        err = f"Exit code {p.returncode}"
    return p.stdout.rstrip("\n"), err


def message(command: str, out: str, err: str) -> str:
    """The command and its output as the session is told them: Claude Code's
    two bash-mode records, as one message."""
    return (f"<bash-input>{command}</bash-input>\n"
            f"<bash-stdout>{_cap(out)}</bash-stdout><bash-stderr>{_cap(err)}</bash-stderr>")


def parse(text: str) -> dict | None:
    """`{"command"?, "stdout"?, "stderr"?}` of a user record that is bash
    mode's (either record, or both in one), else None. Only a record that is
    nothing else: tags quoted inside someone's sentence are that sentence."""
    text = (text or "").strip()
    found = {m.group(1): m.group(2) for m in _TAGS.finditer(text)}
    if not found or _TAGS.sub("", text).strip():
        return None
    got = {}
    if "bash-input" in found:
        got["command"] = found["bash-input"].strip()
    for tag, key in (("bash-stdout", "stdout"), ("bash-stderr", "stderr")):
        if tag in found:
            got[key] = found[tag].rstrip("\n")
    return got


def run_then_send(session: str, command: str, cwd: str, send) -> None:
    """Run `command` in the background, then `send(text)` the result into
    `session`. The reply is answered at once: a command can take two minutes."""

    def go() -> None:
        out, err = run(command, cwd)
        try:
            send(message(command, out, err))
        except Exception as e:  # noqa: BLE001 — nobody is waiting on this
            print(f"shell: could not send {session[:8]} its output ({e})", file=sys.stderr)

    threading.Thread(target=go, daemon=True, name=f"shell-{session[:8]}").start()
