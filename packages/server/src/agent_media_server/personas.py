"""Personas and roles for a fresh chat (server-contract §6.24, step 1 of
docs/proposals/2026-10-15-personas-in-presets.md).

The personas live in `~/agent-personas` (`MEDIA_PERSONAS_DIR`), and its
`bin/persona-render` is the only thing here that reads them: `--list` names
them, and `<id> --adapter <agent> [--role <role>] --mode public` turns one
into a prompt for that harness. Always public — this module never asks for
`summary` or `private`, so it never reaches `sops`. A host without the repo
has empty lists and no persona chips; that is not an error.

A render is delivered by the harness's own means (`via`): Claude and pi take
it as an appended system prompt from a file; codex, opencode and Hermes get
it ahead of the first message (`prepend`). The file is 0600, under
`$XDG_RUNTIME_DIR` (else the state dir), and kept — a resumed Claude may be
handed it again — so it is named per session once the session is known.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

#: How long a render or a listing may take.
RENDER_TIMEOUT_S = 20.0
LIST_TIMEOUT_S = 10.0

#: Harnesses that take a system prompt from a file, and the flag that does it.
#: pi's `--append-system-prompt` reads a path as the file's contents.
SYSTEM_PROMPT_FLAG = {"claude": "--append-system-prompt-file",
                      "pi": "--append-system-prompt"}


def repo_dir() -> Path:
    raw = (os.environ.get("MEDIA_PERSONAS_DIR") or "").strip()
    return Path(os.path.expanduser(raw or "~/agent-personas"))


def renderer() -> Path:
    return repo_dir() / "bin" / "persona-render"


def _command(windows: bool = os.name == "nt") -> list[str]:
    """How to run the renderer. Windows cannot run a script by its shebang
    (WinError 193, "not a valid Win32 application"), and persona-render is a
    stdlib-only Python script (`#!/usr/bin/env python3`): there it is handed
    to this server's own Python."""
    exe = renderer()
    if windows:
        try:
            with exe.open("rb") as fh:
                first = fh.readline(256)
        except OSError:
            first = b""
        if first.startswith(b"#!") and b"python" in first.lower():
            return [sys.executable, str(exe)]
    return [str(exe)]


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run([*_command(), *args], cwd=str(repo_dir()),
                          capture_output=True, text=True, timeout=timeout)


def listing() -> dict:
    """`{"personas": [...], "roles": [...]}` from `persona-render --list`;
    both empty when the repo (or its renderer) is not there or fails."""
    out: dict[str, list[str]] = {"personas": [], "roles": []}
    if not renderer().is_file():
        return out
    try:
        r = _run(["--list"], LIST_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return out
    if r.returncode != 0:
        return out
    section = ""
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        if not line[:1].isspace() and line.rstrip().endswith(":"):
            section = line.strip()[:-1]
            continue
        if section in out:
            out[section].append(line.strip())
    return out


def check(persona: str, role: str, lists: dict | None = None) -> str:
    """Why `persona`/`role` cannot be used, or "" — an id the listing does not
    name (which is every id on a host without the repo)."""
    if not persona and not role:
        return ""
    lists = lists if lists is not None else listing()
    if persona and persona not in lists["personas"]:
        return f"unknown persona {persona!r}"
    if role and role not in lists["roles"]:
        return f"unknown role {role!r}"
    return ""


def render(persona: str, role: str, agent: str) -> tuple[bool, str]:
    """The public render for `agent`: `(True, text)` or `(False, why)`.
    A role on its own is rendered as its own id (`persona-render reviewer`)."""
    args = [persona or role, "--adapter", agent, "--mode", "public"]
    if persona and role:
        args += ["--role", role]
    try:
        r = _run(args, RENDER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, f"persona-render took over {RENDER_TIMEOUT_S:.0f}s"
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"persona-render failed ({e})"
    if r.returncode != 0 or not r.stdout.strip():
        lines = [ln for ln in (r.stderr or r.stdout or "").splitlines() if ln.strip()]
        return False, lines[-1].strip() if lines else f"persona-render exited {r.returncode}"
    return True, r.stdout.strip() + "\n"


def via(agent: str) -> str:
    """How `agent` gets a render: "system_prompt" or "prepend"."""
    return "system_prompt" if agent in SYSTEM_PROMPT_FLAG else "prepend"


def prepend(render_text: str, text: str) -> str:
    """The first message for a harness without a system-prompt route."""
    return f"# Persona\n\n{render_text.strip()}\n\n---\n\n{text}".rstrip() + "\n"


def files_dir() -> Path:
    run = (os.environ.get("XDG_RUNTIME_DIR") or "").strip()
    if run and os.path.isdir(run):
        return Path(run) / "agent-media" / "personas"
    from agent_media_core import harness_profiles

    return harness_profiles.state_dir() / "personas"


def write(render_text: str) -> Path:
    """The render in a 0600 file of its own; `name_for` gives it the
    session's name once that is known."""
    d = files_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = d / f"{uuid.uuid4().hex}.md"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(render_text)
    return path


def name_for(path: Path, session: str) -> None:
    """A second name, `<session>.md`, for the file a session was started with
    (a hard link: the harness keeps the path it was given)."""
    if not session:
        return
    try:
        os.link(path, path.with_name(f"{session}.md"))
    except OSError:
        pass
