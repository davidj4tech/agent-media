"""The folders a new project may be made in (server-contract §6.24, "Project roots").

A new project is `<root>/<name>` (new_project.create). The roots are a list
kept in `<state_dir>/project-roots.json` — `["/home/…/projects", …]`, the
first the default — and with no file it is the one folder there always was:
MEDIA_PROJECTS_DIR, else ~/projects. That default is the host's own word and
is not judged; a root added from the app must be an existing directory under
the home folder, judged after resolving symlinks, so a link in ~ cannot point
a project at /etc.

This is tidiness, not a boundary: any paired device may start an agent that
runs without permission checks, wherever it runs. The boundary is the role
of the device that asks (docs/proposals/2026-10-18-roles.md); until roles
exist `auth.may_admin` lets every device edit this list.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import auth


def store_path() -> Path:
    from agent_media_core import harness_profiles

    return harness_profiles.state_dir() / "project-roots.json"


def _home() -> Path:
    return Path(os.path.expanduser("~")).resolve()


def default_root() -> str:
    from . import new_project

    return str(new_project.root())


def label(path: str) -> str:
    """`~/projects` for `/home/d/projects`; the path itself outside home."""
    home = str(_home())
    if path == home:
        return "~"
    if path.startswith(home.rstrip("/") + "/"):
        return "~/" + path[len(home.rstrip("/")) + 1:]
    return path


def load() -> list[str]:
    try:
        rows = json.loads(store_path().read_text())
    except (OSError, ValueError):
        rows = None
    roots = [str(r) for r in rows if isinstance(r, str) and r.strip()] if isinstance(rows, list) else []
    return list(dict.fromkeys(roots)) or [default_root()]


def _save(roots: list[str]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(roots, indent=1) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def listing() -> list[dict]:
    roots = load()
    return [{"path": r, "label": label(r), "default": i == 0} for i, r in enumerate(roots)]


def find(path: str) -> str | None:
    """The listed root `path` names — as listed, or by its resolved form — else None."""
    path = (path or "").strip()
    if not path:
        return None
    roots = load()
    if path in roots:
        return path
    try:
        resolved = str(Path(os.path.expanduser(path)).resolve())
    except (OSError, RuntimeError):
        return None
    return next((r for r in roots if r == resolved or _resolve(r) == resolved), None)


def _resolve(path: str) -> str:
    try:
        return str(Path(path).resolve())
    except (OSError, RuntimeError):
        return path


def _judge(raw: str) -> tuple[str, dict]:
    """`(resolved path, {})` for a folder that may be a root, else `("", error)`."""
    raw = (raw or "").strip()
    if not raw:
        return "", {"error": "no path", "status": 400}
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = _home() / p
    try:
        resolved = p.resolve(strict=True)
    except (OSError, RuntimeError):
        return "", {"error": f"{raw!r} does not exist", "status": 400}
    if not resolved.is_dir():
        return "", {"error": f"{raw!r} is not a folder", "status": 400}
    home = _home()
    if resolved != home and home not in resolved.parents:
        return "", {"error": f"{raw!r} is not under the home folder", "status": 400}
    return str(resolved), {}


def _answer() -> dict:
    return {"roots": listing()}


def get(bearer: str) -> tuple[bool, dict]:
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    return True, _answer()


def add(path: str, bearer: str) -> tuple[bool, dict]:
    user, err = auth.may_admin(bearer)
    if not user:
        return False, err
    resolved, why = _judge(path)
    if not resolved:
        return False, why
    roots = load()
    if find(resolved):
        return False, {"error": f"{label(resolved)} is already listed", "status": 409}
    _save(roots + [resolved])
    return True, _answer()


def remove(path: str, bearer: str) -> tuple[bool, dict]:
    user, err = auth.may_admin(bearer)
    if not user:
        return False, err
    hit = find(path)
    if not hit:
        return False, {"error": f"{path!r} is not a project folder here", "status": 404}
    roots = load()
    if len(roots) == 1:
        return False, {"error": "the last project folder cannot be removed", "status": 409}
    _save([r for r in roots if r != hit])
    return True, _answer()


def make_default(path: str, bearer: str) -> tuple[bool, dict]:
    user, err = auth.may_admin(bearer)
    if not user:
        return False, err
    hit = find(path)
    if not hit:
        return False, {"error": f"{path!r} is not a project folder here", "status": 404}
    _save([hit] + [r for r in load() if r != hit])
    return True, _answer()
