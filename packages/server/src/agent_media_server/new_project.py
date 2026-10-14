"""A new project from the app, or a new worktree of one (server-contract §6.24).

There is no project record to write (see `sessions.project_of`): a project is
the folder a thread ran in. So "new project" is a folder that did not exist,
made here, and a first chat opened in it — `/ask` with `new_project`, the one
call, so a folder never appears without the chat that asked for it.

The root is MEDIA_PROJECTS_DIR, else ~/projects. A name is one path segment of
letters, digits, `.`, `_` and `-`, starting with a letter or digit; a folder
already there is refused (409) rather than adopted — an existing project is
picked from the places list, not created twice. `git init` (or a clone of
`repo`) because a git root is what scopes the docs list
(`docs.project_root`) and lets the chat commit from its first turn; a host
with no git still gets the folder, but not a clone.

A worktree goes inside the project it is cut from, `<repo>/.worktrees/<branch>`,
so `project_of` files its threads under that project.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

#: What a clone may be given: a URL git fetches over the network, never a
#: local path or a `file://`/`ext::` transport that runs something here.
REPO = re.compile(r"(?:https://|ssh://)[^\s]+|[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s]+")

CLONE_TIMEOUT = 300

#: AGENTS.md when neither a preset nor the host's template says otherwise.
AGENTS_MD = """# {name}

Instructions for coding agents working in this project.

## What this is

(Describe the project: its purpose, its stack, how to run it and test it.)

## Conventions

- Keep changes small and explain why in the commit message.
"""


#: The opening words when AGENTS.md is to be drafted rather than templated
#: (`agents_md: "draft"`): every harness can follow them, where `/init` is
#: each one's own command or missing. Ahead of whatever the listener typed.
DRAFT_PROMPT = (
    "Before anything else, read this repository and write AGENTS.md at its root: "
    "what the project is, its layout, how to build, run and test it, and the "
    "conventions its code and commits follow, all taken from the code itself, not "
    "guessed. Keep it short, for a coding agent new to the project. If AGENTS.md or "
    "CLAUDE.md is already there, improve it rather than replace it. CLAUDE.md is a "
    "link to AGENTS.md; leave it so. Then stop and tell me what you wrote.")


def root() -> Path:
    return Path(os.path.expanduser(
        (os.environ.get("MEDIA_PROJECTS_DIR") or "").strip() or "~/projects"))


def template_path() -> Path:
    from agent_media_core import config

    return config.config_path().parent / "agents-template.md"


def default_agents_md() -> str:
    """The host's template, else the built-in one."""
    try:
        text = template_path().read_text()
    except OSError:
        text = ""
    return text if text.strip() else AGENTS_MD


def _git(*args: str, timeout: float = 20) -> tuple[bool, str]:
    git = shutil.which("git")
    if not git:
        return False, "git is not installed"
    try:
        r = subprocess.run([git, *args], capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired:
        return False, f"git {args[0]} took over {int(timeout)}s"
    except OSError as e:
        return False, str(e)
    lines = (r.stderr or r.stdout or "").strip().splitlines()
    return r.returncode == 0, lines[-1] if lines else ""


def _write_agents_md(path: Path, name: str, text: str, draft: bool = False) -> None:
    agents = path / "AGENTS.md"
    if not agents.exists() and not draft:
        agents.write_text((text or default_agents_md()).replace("{name}", name))
    # Drafting, the link waits (dangling) for the agent to write AGENTS.md.
    claude = path / "CLAUDE.md"
    if not claude.exists() and not claude.is_symlink():
        claude.symlink_to("AGENTS.md")


def create(name: str, repo: str = "", agents_md: bool | str = False,
           agents_text: str = "") -> tuple[bool, dict]:
    """Make `<root>/<name>`: a clone of `repo`, or a `git init`, with a starter
    AGENTS.md when `agents_md` — or, when it is "draft", only CLAUDE.md → it,
    for the first chat to write (DRAFT_PROMPT). `(True, {"cwd"})`, or a
    refusal with its status."""
    name = (name or "").strip()
    repo = (repo or "").strip()
    if not NAME.fullmatch(name) or name.endswith(".git"):
        return False, {"error": "a project name is letters, digits, '.', '_' and '-'",
                       "status": 400}
    if repo and (not REPO.fullmatch(repo) or repo.startswith("-")):
        return False, {"error": "a repo is an https://, ssh:// or user@host:path URL",
                       "status": 400}
    path = root() / name
    if path.exists() or path.is_symlink():
        return False, {"error": f"{name!r} already exists", "status": 409, "cwd": str(path)}
    try:
        root().mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, {"error": f"could not make {root()}: {e}", "status": 500}
    if repo:
        ok, why = _git("clone", "--quiet", "--", repo, str(path), timeout=CLONE_TIMEOUT)
        if not ok:
            shutil.rmtree(path, ignore_errors=True)
            return False, {"error": f"clone failed: {why}", "status": 502}
    else:
        try:
            path.mkdir()
        except FileExistsError:
            return False, {"error": f"{name!r} already exists", "status": 409, "cwd": str(path)}
        except OSError as e:
            return False, {"error": f"could not make {path}: {e}", "status": 500}
        ok, why = _git("init", "-q", str(path))
        if not ok:
            print(f"new project: git init {path} failed ({why})", file=sys.stderr)
    if agents_md:
        try:
            _write_agents_md(path, name, agents_text, draft=agents_md == "draft")
        except OSError as e:
            print(f"new project: AGENTS.md in {path} not written ({e})", file=sys.stderr)
    return True, {"cwd": str(path)}


def worktree(source: str, branch: str) -> tuple[bool, dict]:
    """`git worktree add -b <branch>` of `source` (a place, checked by the
    caller) at `<source>/.worktrees/<branch>`. `(True, {"cwd", "branch"})`."""
    source = (source or "").rstrip("/")
    branch = (branch or "").strip()
    ok, top = _git("-C", source, "rev-parse", "--show-toplevel")
    if not ok or not top:
        return False, {"error": f"{source!r} is not a git repository", "status": 422}
    top_path = Path(top)
    if not branch or branch.startswith("-") or not _git("check-ref-format", "--branch", branch)[0]:
        return False, {"error": f"{branch!r} is not a branch name", "status": 400}
    if _git("-C", top, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")[0]:
        return False, {"error": f"branch {branch!r} already exists", "status": 409}
    dest = top_path / ".worktrees" / branch
    if dest.exists():
        return False, {"error": f"{dest} already exists", "status": 409, "cwd": str(dest)}
    ok, why = _git("-C", top, "worktree", "add", "--quiet", "-b", branch, str(dest), timeout=120)
    if not ok:
        return False, {"error": f"worktree failed: {why}", "status": 502}
    _exclude(top_path)
    return True, {"cwd": str(dest), "branch": branch}


def _exclude(top: Path) -> None:
    """`.worktrees/` in the repo's own ignore list, so the project's status
    does not list its worktrees as untracked."""
    ok, git_dir = _git("-C", str(top), "rev-parse", "--git-common-dir")
    if not ok or not git_dir:
        return
    info = (top / git_dir) / "info" / "exclude"
    try:
        lines = info.read_text().splitlines() if info.exists() else []
        if "/.worktrees/" not in lines:
            info.parent.mkdir(parents=True, exist_ok=True)
            with info.open("a") as fh:
                fh.write(("\n" if lines and lines[-1] else "") + "/.worktrees/\n")
    except OSError as e:
        print(f"worktree: could not exclude .worktrees in {top} ({e})", file=sys.stderr)
