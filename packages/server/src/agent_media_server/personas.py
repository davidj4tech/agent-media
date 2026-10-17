"""Personas and roles for a fresh chat (server-contract §6.24, step 1 of
docs/proposals/2026-10-15-personas-in-presets.md).

The personas live in `~/agent-personas` (`MEDIA_PERSONAS_DIR`), and its
`bin/persona-render` is the only thing here that reads them: `--list --json`
describes them, and `<id> --adapter <agent> [--role <role>] --mode public`
turns one into a prompt for that harness. Where each is offered, and which
models are trusted with a private one, is persona_scope.py's. Public only
while PRIVATE_MODE_READY is off: then this module never asks for `summary`
or `private`, so it never reaches `sops`. A host without the repo has empty
lists and no persona chips; that is not an error.

A render is delivered by the harness's own means (`via`): Claude and pi take
it as an appended system prompt from a file; codex, opencode and Hermes get
it ahead of the first message (`prepend`). The file is 0600, under
`$XDG_RUNTIME_DIR` (else the state dir), and kept — a resumed Claude may be
handed it again — so it is named per session once the session is known.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

#: How long a render or a listing may take.
RENDER_TIMEOUT_S = 20.0
LIST_TIMEOUT_S = 10.0

#: Private renders: on now that a private thread is kept out of search, the
#: shelf and feeds, recaps, titles, figures, follow-ups and catch-ups
#: (agent_media_core.private), out of the governor and fleet-sync (agent-memory,
#: agent-sessions), and sealed when it ends (reap.seal_private). 17 Oct 2026.
PRIVATE_MODE_READY = True

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


def private_ids(ids: list[str]) -> list[str]:
    """The personas among `ids` with private material in the repo's
    `private/` — a `<id>.sops.md` or `<id>-….sops.md` there (only its name is
    read, never what is in it). The app keeps them behind a lock (§6.24)."""
    try:
        names = [f.name for f in (repo_dir() / "private").iterdir()]
    except OSError:
        return []
    return [i for i in ids
            if any(n.startswith((f"{i}.", f"{i}-")) for n in names)]


def _text_listing() -> dict[str, list[str]]:
    """Ids from the plain `--list`, for a renderer older than `--json`."""
    out: dict[str, list[str]] = {"personas": [], "roles": []}
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


def described() -> list[dict]:
    """Every persona and role as `persona-render --list --json` gives it:
    `{"id", "kind", "adapters", "projects", "private"}`. A renderer older
    than `--json` gives ids only: no limits, and `private` by file name
    (`private_ids`). Empty without the repo or when the renderer fails."""
    if not renderer().is_file():
        return []
    try:
        r = _run(["--list", "--json"], LIST_TIMEOUT_S)
        data = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        data = None
    if isinstance(data, dict):
        return [{"id": str(d.get("id")), "kind": kind[:-1], "adapters": d.get("adapters"),
                 "projects": d.get("projects"), "private": d.get("private") is True}
                for kind in ("personas", "roles") for d in data.get(kind) or []
                if isinstance(d, dict) and d.get("id")]
    lists = _text_listing()
    private = set(private_ids(lists["personas"]))
    return [{"id": i, "kind": kind[:-1], "adapters": None, "projects": None, "private": i in private}
            for kind in ("personas", "roles") for i in lists[kind]]


def listing(rows: list[dict] | None = None) -> dict:
    """`{"personas": [...], "roles": [...], "private": [...]}`, `private` the
    personas with private material; all empty without the repo."""
    rows = described() if rows is None else rows
    return {"personas": [d["id"] for d in rows if d["kind"] == "persona"],
            "roles": [d["id"] for d in rows if d["kind"] == "role"],
            "private": [d["id"] for d in rows if d["kind"] == "persona" and d["private"]]}


def check(persona: str, role: str, lists: dict | None = None, agent: str = "",
          rows: list[dict] | None = None) -> str:
    """Why `persona`/`role` cannot be used, or "" — an id the listing does not
    name (which is every id on a host without the repo), or, given `agent`,
    one its scope keeps from that agent (persona_scope.py)."""
    if not persona and not role:
        return ""
    if lists is None:
        rows = described() if rows is None else rows
        lists = listing(rows)
    if persona and persona not in lists["personas"]:
        return f"unknown persona {persona!r}"
    if role and role not in lists["roles"]:
        return f"unknown role {role!r}"
    if agent and rows is not None:
        from . import persona_scope

        scope = persona_scope.resolve(rows)["scope"]
        for pid in (persona, role):
            if pid and not persona_scope.takes(scope, pid, agent):
                return f"{pid} is not offered for {agent}"
    return ""


def mode_for(persona: str, model: str, rows: list[dict], agent: str = "") -> str:
    """The render mode for `persona` on `model`: "private" for a persona with
    private material on a trusted model once PRIVATE_MODE_READY, else
    "public". Only for an agent that takes it as a system prompt (`via`):
    elsewhere it would be the thread's first message, and Hermes and
    opencode, which could not be sealed after, are among those."""
    from . import persona_scope

    if not persona or not PRIVATE_MODE_READY or via(agent) != "system_prompt":
        return "public"
    resolved = persona_scope.resolve(rows)
    row = resolved["scope"].get(persona) or {}
    return "private" if row.get("private") and persona_scope.trusted(model, resolved["trusted_models"]) else "public"


def render(persona: str, role: str, agent: str, mode: str = "public") -> tuple[bool, str]:
    """The render for `agent` in `mode` (`mode_for`): `(True, text)` or
    `(False, why)`. A role on its own is rendered as its own id
    (`persona-render reviewer`)."""
    args = [persona or role, "--adapter", agent, "--mode", mode]
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


def forget(session: str) -> int:
    """Delete the render `session` was started with: `<session>.md` and every
    other name of the same file. The number of names removed. For a private
    thread once it is sealed — its render is the decrypted persona."""
    d = files_dir()
    own = d / f"{session}.md"
    try:
        ino = own.stat().st_ino
    except OSError:
        return 0
    gone = 0
    for p in d.iterdir():
        try:
            if p.stat().st_ino == ino:
                p.unlink()
                gone += 1
        except OSError:
            pass
    return gone


def name_for(path: Path, session: str) -> None:
    """A second name, `<session>.md`, for the file a session was started with
    (a hard link: the harness keeps the path it was given)."""
    if not session:
        return
    try:
        os.link(path, path.with_name(f"{session}.md"))
    except OSError:
        pass
