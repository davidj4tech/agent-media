"""The folders a new project may be made in (server-contract §6.24, "Project roots").

A new project is `<root>/<name>` (new_project.create). The roots are a list
kept in `<state_dir>/project-roots.json` — `["/home/…/projects", …]`, the
first the default — and with no file it is the one folder there always was:
MEDIA_PROJECTS_DIR, else ~/projects. That default is the host's own word and
is not judged; a root added from the app must be an existing directory under
the home folder, judged after resolving symlinks, so a link in ~ cannot point
a project at /etc.

A listed root whose folder has gone (deleted, an unmounted disk) stays
listed, marked `"missing": true` — judged at each answer, so it is simply
present again when the folder comes back — and a new project is never made
in it (`chosen`: 409 "that folder is gone"); a new project never recreates
it. Removing it still works, and so does recreating it — explicitly. `default` stays the stored first one even when it
is missing: the app starts on the first present root and names it outright.

A folder that does not exist yet may be made from Settings, never silently:
`add` without `create` answers 404 with `would_create` (what `mkdir -p`
would make, top first), with `create` it makes them; `recreate` remakes a
listed root that has gone. Both go through `_plan`: the nearest existing
ancestor must resolve to a directory under home (no file in the way, no link
out of it), the target may not be home itself or under the state dir, and at
most MAX_NEW levels are made. Folders get mkdir's own mode (0777 less the
umask), as `mkdir -p` would.

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


def present(path: str) -> bool:
    """The root's folder is there: it exists and is a directory (symlinks followed)."""
    try:
        return Path(path).is_dir()
    except OSError:
        return False


GONE = "that folder is gone"


def listing() -> list[dict]:
    roots = load()
    return [{"path": r, "label": label(r), "default": i == 0, "missing": not present(r)}
            for i, r in enumerate(roots)]


def chosen(root: str = "") -> tuple[str, dict]:
    """The root a new project goes in: `root` as listed (else 400), or the
    default when it is left out — `(path, {})`, or `("", refusal)`; a root
    whose folder is gone → 409, the default included (no fallback: the app
    names the present one it shows)."""
    base = find(root) if (root or "").strip() else load()[0]
    if not base:
        return "", {"error": f"{root!r} is not one of the project folders", "status": 400}
    if not present(base):
        return "", {"error": GONE, "status": 409, "root": base}
    return base, {}


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


def _absolute(raw: str) -> Path:
    """`raw` with `~` expanded; a relative path is taken from home."""
    p = Path(os.path.expanduser(raw))
    return p if p.is_absolute() else _home() / p


def _judge(raw: str) -> tuple[str, dict]:
    """`(resolved path, {})` for a folder that may be a root, else `("", error)`."""
    raw = (raw or "").strip()
    if not raw:
        return "", {"error": "no path", "status": 400}
    p = _absolute(raw)
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


MAX_NEW = 4


def _plan(raw: str) -> tuple[Path | None, list[Path], dict]:
    """For a path that does not exist: `(target, [folders to make, top first], {})`,
    else `(None, [], error)`. Judged from the nearest existing ancestor, after
    resolving it, so a link cannot carry the new folders out of home."""
    raw = (raw or "").strip()
    if not raw:
        return None, [], {"error": "no path", "status": 400}
    p = _absolute(raw)
    tail: list[str] = []
    base = p
    while not os.path.lexists(base):
        if base.parent == base:
            return None, [], {"error": f"{raw!r} has no existing folder above it", "status": 400}
        tail.insert(0, base.name)
        base = base.parent
    if any(part in ("", ".", "..") for part in tail):
        return None, [], {"error": f"{raw!r}: no '..' in the folders to be made", "status": 400}
    try:
        resolved = base.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, [], {"error": f"{label(str(base))} is a broken link", "status": 400}
    if not resolved.is_dir():
        return None, [], {"error": f"{label(str(base))} is in the way: it is not a folder", "status": 400}
    home = _home()
    if resolved != home and home not in resolved.parents:
        return None, [], {"error": f"{raw!r} is not under the home folder", "status": 400}
    target = resolved.joinpath(*tail)
    if target == home:
        return None, [], {"error": "the home folder itself cannot be a project folder", "status": 400}
    from agent_media_core import harness_profiles

    state = Path(os.path.realpath(harness_profiles.state_dir()))
    if target == state or state in target.parents:
        return None, [], {"error": f"{raw!r} is inside the server's own state folder", "status": 400}
    if len(tail) > MAX_NEW:
        return None, [], {"error": f"{raw!r} would make {len(tail)} new folders; at most {MAX_NEW}",
                          "status": 400}
    return target, [resolved.joinpath(*tail[:i + 1]) for i in range(len(tail))], {}


def _make(new: list[Path]) -> dict:
    """mkdir each, top first — `{}`, or an error (a race put something in the way)."""
    for d in new:
        try:
            os.mkdir(d)
        except FileExistsError:
            if not d.is_dir() or d.is_symlink():
                return {"error": f"{label(str(d))} appeared and is in the way", "status": 409}
        except OSError as e:
            return {"error": f"could not make {label(str(d))}: {e.strerror or e}", "status": 400}
    return {}


def _answer() -> dict:
    return {"roots": listing()}


def get(bearer: str) -> tuple[bool, dict]:
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    return True, _answer()


def add(path: str, bearer: str, create: bool = False) -> tuple[bool, dict]:
    """Add a root. One that does not exist: without `create` 404 with
    `would_create` (labels, top first) when it could be made, with it made
    first (`made` in the answer)."""
    user, err = auth.may_admin(bearer)
    if not user:
        return False, err
    made: list[str] = []
    raw = (path or "").strip()
    if raw and not os.path.lexists(_absolute(raw)):
        target, new, why = _plan(raw)
        if target is None:
            return False, why
        if find(str(target)):
            return False, {"error": f"{label(str(target))} is already listed", "status": 409}
        if not create:
            return False, {"error": f"{raw!r} does not exist", "status": 404,
                           "would_create": [label(str(d)) for d in new]}
        why = _make(new)
        if why:
            return False, why
        made = [label(str(d)) for d in new]
        path = str(target)
    resolved, why = _judge(path)
    if not resolved:
        return False, why
    roots = load()
    if find(resolved):
        return False, {"error": f"{label(resolved)} is already listed", "status": 409}
    _save(roots + [resolved])
    return True, {**_answer(), "made": made}


def recreate(path: str, bearer: str) -> tuple[bool, dict]:
    """Remake a listed root whose folder has gone (mkdir -p, judged as `add`'s
    create) — the explicit act itself, so no would_create step. `made` lists
    what it made; [] when the folder is there already."""
    user, err = auth.may_admin(bearer)
    if not user:
        return False, err
    hit = find(path)
    if not hit:
        return False, {"error": f"{path!r} is not a project folder here", "status": 404}
    if present(hit):
        return True, {**_answer(), "made": []}
    target, new, why = _plan(hit)
    if target is None:
        return False, why
    why = _make(new)
    if why:
        return False, why
    return True, {**_answer(), "made": [label(str(d)) for d in new]}


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
