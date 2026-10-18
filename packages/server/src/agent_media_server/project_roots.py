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

The edge cases (David, 18 Oct 2026; docs/proposals/2026-10-18-roles.md):
each stored root keeps the `st_dev`/`st_ino` of its folder, and one now on
another device is `missing` with `why: "unmounted"` (an unmounted drive's
empty mount point), refused as a gone one; recreate refuses when the folder
above is on another device. No root in a git work tree or inside/around
another root; no hidden folder under home, no Windows-reserved name or
trailing dot/space (on every host). Folders are made a level at a time with
mkdirat in an O_NOFOLLOW-opened parent (by path plus a re-check where there
is no dir_fd), and what a failed request made is removed again. Names are
compared after normalising (`_key`: realpath, NFC, case-folded where the
disk folds case), and every edit holds a lock file.

This is tidiness, not a boundary: any paired device may start an agent that
runs without permission checks, wherever it runs. The boundary is the role
of the device that asks (docs/proposals/2026-10-18-roles.md); until roles
exist `auth.may_admin` lets every device edit this list.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import threading
import time
import unicodedata
from pathlib import Path

from . import auth

try:  # POSIX
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]


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


# --- the store: rows of {"path", "dev", "ino"}, under a lock -------------

class _Lock:
    """An exclusive lock on `project-roots.json.lock` around a read-modify-write
    (flock on POSIX, msvcrt.locking on Windows), re-entrant within a thread so
    `load`'s migration may run inside an edit. Other threads and processes
    wait (Windows: up to LOCK_WAIT seconds, then OSError)."""

    _held = threading.local()

    def __enter__(self):
        n = getattr(self._held, "n", 0)
        self._held.n = n + 1
        if n:
            return self
        self.fd = -1
        try:
            path = store_path().with_name("project-roots.json.lock")
            path.parent.mkdir(parents=True, exist_ok=True)
            self.fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_EX)
            elif msvcrt is not None:
                deadline = time.monotonic() + LOCK_WAIT
                while True:
                    try:
                        msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(0.05)
        except BaseException:
            self._release()
            raise
        return self

    def _release(self):
        self._held.n = 0
        if getattr(self, "fd", -1) >= 0:
            try:
                if fcntl is not None:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
                elif msvcrt is not None:
                    os.lseek(self.fd, 0, 0)
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = -1

    def __exit__(self, *exc):
        if self._held.n > 1:
            self._held.n -= 1
        else:
            self._release()
        return False


LOCK_WAIT = 10.0


def _ident(path: str) -> tuple[int, int] | None:
    """`(st_dev, st_ino)` of the folder at `path` (links followed), else None."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino) if stat.S_ISDIR(st.st_mode) else None


def _row(path: str) -> dict:
    ident = _ident(path)
    return {"path": path, "dev": ident[0], "ino": ident[1]} if ident else {"path": path}


def _read() -> list[dict]:
    """The stored rows (a bare path, the format before 18 Oct 2026, is a row
    with no device yet); [] with no file."""
    try:
        data = json.loads(store_path().read_text())
    except (OSError, ValueError):
        return []
    rows, seen = [], set()
    for r in data if isinstance(data, list) else []:
        row = {"path": r} if isinstance(r, str) else dict(r) if isinstance(r, dict) else {}
        p = row.get("path")
        if isinstance(p, str) and p.strip() and p not in seen:
            seen.add(p)
            rows.append(row)
    return rows


def _rows() -> list[dict]:
    """The rows, the default first; with no file the one folder there always
    was (unrecorded: the host's word). A stored row with no device whose
    folder is there gets it now (migration)."""
    rows = _read()
    if not rows:
        return [{"path": default_root()}]
    if any(r.get("dev") is None and _ident(r["path"]) for r in rows):
        try:
            with _Lock():
                rows = _read() or rows
                todo = [r for r in rows if r.get("dev") is None and _ident(r["path"])]
                for r in todo:
                    r.update(_row(r["path"]))
                if todo:
                    _save(rows)
        except OSError:
            pass
    return rows


def load() -> list[str]:
    return [r["path"] for r in _rows()]


def _save(rows: list[dict]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=1) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


# --- missing, unmounted ------------------------------------------------------

GONE = "that folder is gone"
UNMOUNTED = "that folder's drive is not mounted"


def _why(row: dict) -> str:
    """"" when the root's folder is there; "gone" (not there, or not a
    folder); "unmounted" — there, but on another device than when it was
    added: the empty mount point of a drive not mounted (or something
    mounted over it), where a new project would land on the wrong disk."""
    ident = _ident(row["path"])
    if not ident:
        return "gone"
    if row.get("dev") is not None and ident[0] != row["dev"]:
        return "unmounted"
    return ""


def present(path: str) -> bool:
    """The root's folder is there, on the device it was added on."""
    row = next((r for r in _rows() if r["path"] == path), {"path": path})
    return not _why(row)


def listing() -> list[dict]:
    out = []
    for i, r in enumerate(_rows()):
        why = _why(r)
        item = {"path": r["path"], "label": label(r["path"]), "default": i == 0, "missing": bool(why)}
        if why:
            item["why"] = why
        out.append(item)
    return out


def chosen(root: str = "") -> tuple[str, dict]:
    """The root a new project goes in: `root` as listed (else 400), or the
    default when it is left out — `(path, {})`, or `("", refusal)`; a root
    whose folder is gone or whose drive is not mounted → 409, the default
    included (no fallback: the app names the present one it shows)."""
    base = find(root) if (root or "").strip() else load()[0]
    if not base:
        return "", {"error": f"{root!r} is not one of the project folders", "status": 400}
    row = next((r for r in _rows() if r["path"] == base), {"path": base})
    why = _why(row)
    if why:
        return "", {"error": UNMOUNTED if why == "unmounted" else GONE, "status": 409,
                    "root": base, "why": why}
    return base, {}


# --- the same folder under several names ------------------------------------

def _case_insensitive(path: str) -> bool:
    """Does the filesystem `path` is on fold case? Windows: yes. Elsewhere,
    by experiment: the nearest existing folder with a letter in its name,
    looked up with the case swapped, is the same inode."""
    if os.name == "nt":
        return True
    p = path
    while not os.path.exists(p) and os.path.dirname(p) != p:
        p = os.path.dirname(p)
    while os.path.dirname(p) != p:
        name = os.path.basename(p)
        swapped = name.swapcase()
        if swapped != name:
            try:
                a, b = os.stat(p), os.stat(os.path.join(os.path.dirname(p), swapped))
            except OSError:
                return False
            return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
        p = os.path.dirname(p)
    return False


def _key(path: str) -> str:
    """What two names of one folder share: `~` expanded, from home when
    relative, links in the existing part resolved, `.`/`..`/trailing `/`
    gone, Unicode NFC, case-folded where the filesystem folds case."""
    p = os.path.expanduser((path or "").strip())
    if not os.path.isabs(p):
        p = os.path.join(str(_home()), p)
    p = unicodedata.normalize("NFC", os.path.realpath(p))
    return p.casefold() if _case_insensitive(p) else p


def find(path: str) -> str | None:
    """The listed root `path` names — as listed, or under another name of the
    same folder (`_key`) — else None."""
    path = (path or "").strip()
    if not path:
        return None
    roots = load()
    if path in roots:
        return path
    try:
        want = _key(path)
    except (OSError, ValueError):
        return None
    return next((r for r in roots if _key(r) == want), None)


def _within(inner: str, outer: str) -> bool:
    return inner.startswith(outer.rstrip(os.sep) + os.sep)


# --- what may be a root ------------------------------------------------------

def _absolute(raw: str) -> Path:
    """`raw` with `~` expanded; a relative path is taken from home."""
    p = Path(os.path.expanduser(raw))
    return p if p.is_absolute() else _home() / p


#: Names Windows will not make (with or without an extension), refused on
#: every host so a root's path stays portable.
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10))}


def _bad_name(name: str) -> str:
    if name.split(".", 1)[0].rstrip(" ").upper() in RESERVED:
        return f"{name!r} is a name Windows reserves"
    if name.endswith((".", " ")):
        return f"{name!r} ends in a dot or a space"
    return ""


def _where(target: Path, raw: str) -> dict:
    """{} when the resolved `target` is a place a root may be, else the
    refusal (400): not under home, home itself, inside the state dir, a
    hidden folder on the way (`~/.config`), a reserved or unportable name."""
    home = _home()
    if target != home and home not in target.parents:
        return {"error": f"{raw!r} is not under the home folder", "status": 400}
    if target == home:
        return {"error": "the home folder itself cannot be a project folder", "status": 400}
    from agent_media_core import harness_profiles

    state = Path(os.path.realpath(harness_profiles.state_dir()))
    if target == state or state in target.parents:
        return {"error": f"{raw!r} is inside the server's own state folder", "status": 400}
    parts = target.relative_to(home).parts
    hidden = next((p for p in parts if p.startswith(".")), "")
    if hidden:
        return {"error": f"{raw!r} is in a hidden folder ({hidden}): not a project folder", "status": 400}
    for p in parts:
        bad = _bad_name(p)
        if bad:
            return {"error": f"{raw!r}: {bad}", "status": 400}
    return {}


def _repo_of(target: Path) -> Path | None:
    """The git work tree `target` is in (or is), judged by a `.git` in it or a
    folder above it, short of home (a dotfiles repo in ~ is not a project)."""
    home = _home()
    for p in (target, *target.parents):
        if p == home or home not in p.parents:
            return None
        if os.path.lexists(p / ".git"):
            return p
    return None


def _fit(target: Path, roots: list[str]) -> dict:
    """{} when `target` may join `roots`, else 400: in a git work tree (new
    projects would nest in that repo and be filed under it), or inside or
    around a listed root (one project, two roots)."""
    repo = _repo_of(target)
    if repo:
        what = "is a git repository" if repo == target else f"is inside the git repository {label(str(repo))}"
        return {"error": f"{label(str(target))} {what}: new projects would nest in it", "status": 400}
    mine = _key(str(target))
    for r in roots:
        theirs = _key(r)
        if _within(mine, theirs):
            return {"error": f"{label(str(target))} is inside the project folder {label(r)}", "status": 400}
        if _within(theirs, mine):
            return {"error": f"{label(str(target))} holds the project folder {label(r)}", "status": 400}
    return {}


def _judge(raw: str) -> tuple[str, dict]:
    """`(resolved path, {})` for an existing folder that may be a root, else `("", error)`."""
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
    why = _where(resolved, raw)
    if why:
        return "", why
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
    target = resolved.joinpath(*tail)
    why = _where(target, raw)
    if why:
        return None, [], why
    if len(tail) > MAX_NEW:
        return None, [], {"error": f"{raw!r} would make {len(tail)} new folders; at most {MAX_NEW}",
                          "status": 400}
    return target, [resolved.joinpath(*tail[:i + 1]) for i in range(len(tail))], {}


# --- making folders: one level at a time, undone on failure ------------------

def _dir_fd_ok() -> bool:
    return (os.mkdir in os.supports_dir_fd and os.open in os.supports_dir_fd
            and os.rmdir in os.supports_dir_fd
            and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"))


#: mkdir relative to an open, link-free parent (see `_make`); off on Windows.
DIR_FD = _dir_fd_ok()


def _is_link(path: str | Path) -> bool:
    """A symlink, or (Windows) any reparse point — a junction is not `islink`."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _failed(d: Path, e: OSError) -> dict:
    """The answer for a mkdir that failed — never a 500."""
    what = f"could not make {label(str(d))}"
    if e.errno in (errno.EACCES, errno.EPERM):
        return {"error": f"{what}: no permission there", "status": 403}
    if e.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
        return {"error": f"{what}: the disk is full", "status": 507}
    if e.errno == errno.EROFS:
        return {"error": f"{what}: the disk is read-only", "status": 409}
    return {"error": f"{what}: {e.strerror or e}", "status": 400}


def _swapped(d: Path) -> dict:
    return {"error": f"{label(str(d))} changed while being made (a link or a file in the way); "
                     "nothing was made", "status": 409}


def _make(new: list[Path]) -> tuple[list[Path], dict]:
    """mkdir each, top first — `(made, {})` with what this call made (a level
    already there as a real folder is used, not counted), or `([], error)`
    with every level this call made removed again, deepest first."""
    return _make_at(new) if DIR_FD else _make_plain(new)


_DIR_FLAGS = getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_DIRECTORY", 0)


def _make_at(new: list[Path]) -> tuple[list[Path], dict]:
    """Each level made with mkdirat in the open parent, then opened with
    O_NOFOLLOW and fstat'ed: a link swapped in between check and mkdir is
    refused, never followed. The existing parent is walked down from `/` the
    same way (it was resolved, so has no links)."""
    flags = _DIR_FLAGS | os.O_NOFOLLOW
    parent = new[0].parent
    fds: list[int] = []
    made: list[tuple[int, str]] = []

    def undo() -> None:
        for fd, name in reversed(made):
            try:
                os.rmdir(name, dir_fd=fd)
            except OSError:
                pass

    try:
        try:
            fds.append(os.open(parent.anchor, _DIR_FLAGS))
            for part in parent.parts[1:]:
                fds.append(os.open(part, flags, dir_fd=fds[-1]))
        except OSError:
            return [], _swapped(parent)
        for d in new:
            try:
                os.mkdir(d.name, dir_fd=fds[-1])
                made.append((fds[-1], d.name))
            except FileExistsError:
                pass   # opened below without following: must be a real folder
            except OSError as e:
                undo()
                return [], _failed(d, e)
            try:
                fd = os.open(d.name, flags, dir_fd=fds[-1])
            except OSError:
                undo()
                return [], _swapped(d)
            fds.append(fd)
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                undo()
                return [], _swapped(d)
        return new[len(new) - len(made):], {}   # what was there already is the top
    finally:
        for fd in fds:
            os.close(fd)


def _make_plain(new: list[Path]) -> tuple[list[Path], dict]:
    """No dir_fd (Windows): mkdir by path, then check each level is a real
    folder (not a link or junction) where it was meant to be."""
    made: list[Path] = []

    def undo() -> None:
        for d in reversed(made):
            try:
                os.rmdir(d)
            except OSError:
                pass

    for d in new:
        try:
            os.mkdir(d)
            made.append(d)
        except FileExistsError:
            pass
        except OSError as e:
            undo()
            return [], _failed(d, e)
        if _is_link(d) or not d.is_dir() or os.path.normcase(os.path.realpath(d)) != os.path.normcase(str(d)):
            if made and made[-1] == d and _is_link(d):
                made.pop()   # not ours any more: leave it
            undo()
            return [], _swapped(d)
    return made, {}


# --- the routes --------------------------------------------------------------

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
        why = _fit(target, load())
        if why:
            return False, why
        if not create:
            return False, {"error": f"{raw!r} does not exist", "status": 404,
                           "would_create": [label(str(d)) for d in new]}
        done, why = _make(new)
        if why:
            return False, why
        made = [label(str(d)) for d in done]
        path = str(target)
    resolved, why = _judge(path)
    if not resolved:
        return False, why
    with _Lock():
        rows = _rows() if store_path().exists() else [_row(default_root())]
        roots = [r["path"] for r in rows]
        if find(resolved):
            return False, {"error": f"{label(resolved)} is already listed", "status": 409}
        why = _fit(Path(resolved), roots)
        if why:
            return False, why
        _save(rows + [_row(resolved)])
    return True, {**_answer(), "made": made}


def recreate(path: str, bearer: str) -> tuple[bool, dict]:
    """Remake a listed root whose folder has gone (mkdir -p, judged as `add`'s
    create) — the explicit act itself, so no would_create step. `made` lists
    what it made; [] when the folder is there already. Refused (409) when
    the drive is not mounted, or when the folder above it is on another
    device than the root was: it would be made on the wrong disk."""
    user, err = auth.may_admin(bearer)
    if not user:
        return False, err
    hit = find(path)
    if not hit:
        return False, {"error": f"{path!r} is not a project folder here", "status": 404}
    row = next(r for r in _rows() if r["path"] == hit)
    why = _why(row)
    if why == "unmounted":
        return False, {"error": f"{label(hit)}: {UNMOUNTED}; mount it rather than recreate it",
                       "status": 409, "why": why}
    if not why:
        return True, {**_answer(), "made": []}
    target, new, why = _plan(hit)
    if target is None:
        return False, why
    above = _ident(str(new[0].parent))
    if row.get("dev") is not None and above and above[0] != row["dev"]:
        return False, {"error": f"{label(hit)} was on another drive than {label(str(new[0].parent))}: "
                                "recreating it would make it on the wrong disk; mount that drive",
                       "status": 409, "why": "unmounted"}
    done, why = _make(new)
    if why:
        return False, why
    with _Lock():
        rows = _read()
        for r in rows:
            if r["path"] == hit:
                r.update(_row(hit))
        if rows:
            _save(rows)
    return True, {**_answer(), "made": [label(str(d)) for d in done]}


def _edit(path: str, bearer: str, change) -> tuple[bool, dict]:
    user, err = auth.may_admin(bearer)
    if not user:
        return False, err
    with _Lock():
        hit = find(path)
        if not hit:
            return False, {"error": f"{path!r} is not a project folder here", "status": 404}
        rows = _rows()
        rows, why = change(rows, hit)
        if why:
            return False, why
        _save(rows)
    return True, _answer()


def remove(path: str, bearer: str) -> tuple[bool, dict]:
    def change(rows, hit):
        if len(rows) == 1:
            return rows, {"error": "the last project folder cannot be removed", "status": 409}
        return [r for r in rows if r["path"] != hit], {}
    return _edit(path, bearer, change)


def make_default(path: str, bearer: str) -> tuple[bool, dict]:
    def change(rows, hit):
        return ([r for r in rows if r["path"] == hit] + [r for r in rows if r["path"] != hit]), {}
    return _edit(path, bearer, change)
