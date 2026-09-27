"""Harness profiles: more than one login per agent.

A harness profile is a harness plus the directory its credentials, settings
and sessions live in. Every harness already keeps all of that in one place
that one environment variable moves, so a second login is the same agent
run with that variable pointing somewhere else:

    claude    ~/.claude              CLAUDE_CONFIG_DIR
    codex     ~/.codex               CODEX_HOME
    pi        ~/.pi/agent            PI_CODING_AGENT_DIR
    opencode  ~/.local/share         XDG_DATA_HOME (its data is <dir>/opencode)

Hermes is not here: it has profiles of its own, which `harnesses` already
reads (`hermes_stores`).

The default profile of each harness is implicit — its usual directory, name
"" — so nothing changes for anyone who never adds one. The others are kept
in `harness-profiles.json` in agent-media's state dir. A profile is either
made here (a directory under the state dir, which removing it may delete) or
adopted (an existing directory, which removing it never touches).

"Profile" means this and nothing else in this module: not the Sasonica
profile (`media-setup profile`, a machine's wiring) and not a persona.
docs/proposals/2026-09-24-opencode-and-harness-profiles.md.

Standard library only: `harnesses` reads this, and the hooks import that.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

#: The variable that moves each harness's directory.
VARS = {
    "claude": "CLAUDE_CONFIG_DIR",
    "codex": "CODEX_HOME",
    "pi": "PI_CODING_AGENT_DIR",
    "opencode": "XDG_DATA_HOME",
}

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,23}")


@dataclass(frozen=True)
class Profile:
    harness: str
    name: str
    dir: str
    #: An existing directory taken as it was: removing the profile leaves it.
    adopted: bool = False


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base).expanduser() / "agent-media"


def store_path() -> Path:
    return state_dir() / "harness-profiles.json"


def profiles_root() -> Path:
    """Where profiles made here keep their directories."""
    return state_dir() / "harness-profiles"


_CACHE: tuple[float, list[Profile]] = (-1.0, [])


def load() -> list[Profile]:
    """Every profile added here, in the order they were. Read again only when
    the file changes: the hooks ask on every event."""
    global _CACHE
    path = store_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    if mtime == _CACHE[0]:
        return list(_CACHE[1])
    try:
        rows = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    out = []
    for r in rows if isinstance(rows, list) else []:
        try:
            p = Profile(str(r["harness"]), str(r["name"]), str(r["dir"]), bool(r.get("adopted")))
        except (KeyError, TypeError):
            continue
        if p.harness in VARS and NAME.fullmatch(p.name):
            out.append(p)
    _CACHE = (mtime, out)
    return list(out)


def _save(rows: list[Profile]) -> None:
    global _CACHE
    _CACHE = (-1.0, [])      # a write inside the file's mtime resolution
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps([asdict(p) for p in rows], indent=1) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def of(harness: str) -> list[Profile]:
    return [p for p in load() if p.harness == harness]


def get(harness: str, name: str) -> Profile | None:
    return next((p for p in load() if p.harness == harness and p.name == name), None)


def env(harness: str, name: str) -> dict[str, str]:
    """The variable to set for `harness` in profile `name`; {} for the default
    (or a profile that is gone, which then falls back to it)."""
    p = get(harness, name) if name else None
    return {VARS[harness]: p.dir} if p else {}


def dirs(harness: str, default: Path) -> list[tuple[str, Path]]:
    """`(profile, dir)` for every place `harness` keeps its state: the default
    first (`default`, as the caller resolves it), then each profile's."""
    return [("", default), *((p.name, Path(p.dir)) for p in of(harness))]


class ProfileError(ValueError):
    pass


def create(harness: str, name: str, adopt: str = "") -> Profile:
    """Add a profile. Made under the state dir, or `adopt` an existing
    directory (e.g. an `~/.codex-work` made by hand) as it is."""
    if harness not in VARS:
        raise ProfileError(f"{harness!r} has no profiles here"
                           + (" (Hermes keeps its own)" if harness == "hermes" else ""))
    if not NAME.fullmatch(name or "") or name == "default":
        raise ProfileError("a profile's name is lower-case letters, digits and dashes, "
                           "up to 24, and not 'default'")
    if get(harness, name):
        raise ProfileError(f"{harness} already has a profile {name!r}")
    if adopt:
        d = Path(os.path.expanduser(adopt)).resolve()
        if not d.is_dir():
            raise ProfileError(f"no directory at {adopt}")
    else:
        d = profiles_root() / f"{harness}-{name}"
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    if any(Path(p.dir) == d for p in load()):
        raise ProfileError(f"{d} is already a profile's directory")
    p = Profile(harness, name, str(d), adopted=bool(adopt))
    _save([*load(), p])
    return p


def remove(harness: str, name: str, delete: bool = False) -> bool:
    """Forget a profile; with `delete`, remove its directory too — only one
    made here, never an adopted one. Whether there was such a profile."""
    p = get(harness, name)
    if not p:
        return False
    _save([q for q in load() if q != p])
    if delete and not p.adopted:
        d = Path(p.dir)
        root = profiles_root().resolve()
        if d.resolve().parent == root:
            shutil.rmtree(d, ignore_errors=True)
    return True
