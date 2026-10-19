"""Which desk this host has: the one answer every "where does a session go" asks.

    ~/.config/agent-media/config.toml

    layout = "default"                      # or "projects-per-tmux-session"

Two layouts, and only two:

``projects-per-tmux-session``
    David's desk. Every project has its own tmux session, `p-<project>`, and a
    Claude Code SessionStart hook (`tmux-organise-panes`) moves a new pane into
    the session named for its directory; tmux-claude-resume respawns windows
    in unattended sessions. A fresh chat from the phone copies an amux
    registration (`~/.amux/sessions/scratch.env`) and opens in `amux-scratch`;
    a revived one opens in whichever session has a client attached. A
    "project" is a library series, named for the tmux session it ran in.

``default``
    Nothing assumed. Chats from the app run headless when MEDIA_HEADLESS is on;
    when a pane is needed anyway, every one opens as a window in ONE tmux
    session, `sasonica`, with a client held on it (a TUI will not start without
    one). No amux, no hook to move anything, no `p-` names: a "project" is a
    folder, called by its basename, and the folders are the ones transcripts
    say sessions ran in (`/targets` places).

## Precedence

``MEDIA_LAYOUT`` → top-level ``layout`` in config.toml → detected. Detection
says David's layout only when BOTH of its marks are on the host: the amux
registrations directory and the hook that files panes into project sessions
(the SessionStart `tmux-organise-panes` entry in Claude Code's settings, or the
tmux-claude-resume hooks in the tmux config). Either alone is a coincidence;
anything else is `default`. `media-setup init` / `install-hooks` write what
was detected into the file, so a host stops guessing once it is installed.

Not cached, like config.load: a setting edited by hand takes effect on the
next request, and the checks are a stat and two small reads.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import config

DEFAULT = "default"
PROJECTS = "projects-per-tmux-session"
LAYOUTS = (DEFAULT, PROJECTS)
ENV = "MEDIA_LAYOUT"

#: The one tmux session the default layout opens app chats in.
CHAT_TMUX = "sasonica"

#: The amux registration a phone-started chat copies (David's layout).
ASK_SESSION_DEFAULT = "scratch"


@dataclass(frozen=True)
class Layout:
    name: str
    source: str        # "env" | "config" | "detected"
    why: str

    @property
    def projects(self) -> bool:
        return self.name == PROJECTS

    def describe(self) -> str:
        return f"{self.name} ({self.source}: {self.why})"


def _normalise(raw) -> str:
    v = str(raw or "").strip().lower()
    if v in ("projects", "project-per-tmux-session", PROJECTS):
        return PROJECTS
    if v == DEFAULT:
        return DEFAULT
    return ""


# --- detection -------------------------------------------------------------------


def amux_home() -> Path:
    return Path(os.path.expanduser(os.environ.get("CC_HOME") or "~/.amux"))


def _claude_settings() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    return Path(os.path.expanduser(base)) / "settings.json"


def _tmux_configs() -> list[Path]:
    home = Path.home()
    return [home / ".tmux.conf", home / ".config" / "tmux" / "tmux.conf",
            home / ".config" / "tmux" / "tmux.conf.local"]


def _move_hook() -> str:
    """Where the hook that files panes into project sessions is installed, or ""."""
    try:
        data = json.loads(_claude_settings().read_text())
    except (OSError, ValueError):
        data = {}
    for matcher in ((data.get("hooks") or {}).get("SessionStart") or []) if isinstance(data, dict) else []:
        for h in (matcher.get("hooks") or []) if isinstance(matcher, dict) else []:
            if "tmux-organise-panes" in str((h or {}).get("command") or ""):
                return "SessionStart tmux-organise-panes"
    for conf in _tmux_configs():
        try:
            if "tmux-claude-resume" in conf.read_text(errors="replace"):
                return f"tmux-claude-resume in {conf.name}"
        except OSError:
            continue
    return ""


def detect() -> tuple[str, str]:
    """`(layout, why)` from what is installed on this host."""
    reg = amux_home() / "sessions"
    hook = _move_hook()
    if reg.is_dir() and hook:
        return PROJECTS, f"{reg} exists and {hook} is installed"
    if reg.is_dir():
        return DEFAULT, f"{reg} exists but no project-session hook is installed"
    if hook:
        return DEFAULT, f"{hook} is installed but there is no {reg}"
    return DEFAULT, "no amux registrations and no project-session hook"


def current(path: Path | None = None) -> Layout:
    raw = os.environ.get(ENV)
    if raw is not None and raw.strip():
        v = _normalise(raw)
        if v:
            return Layout(v, "env", f"{ENV}={raw.strip()}")
    data = config.load(path)
    if "layout" in data:
        v = _normalise(data.get("layout"))
        if v:
            return Layout(v, "config", f"layout = {v!r} in {path or config.config_path()}")
    name, why = detect()
    return Layout(name, "detected", why)


def name() -> str:
    return current().name


def projects() -> bool:
    return current().projects


# --- the questions ---------------------------------------------------------------


def uses_amux() -> bool:
    """Whether amux registrations say where a phone-started chat goes."""
    return projects()


def _amux_registration(name: str) -> tuple[str, str]:
    """`(CC_DIR, CC_FLAGS)` from `~/.amux/sessions/<name>.env`, or blanks."""
    cwd, flags = "", ""
    try:
        for line in (amux_home() / "sessions" / f"{name}.env").read_text().splitlines():
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if k == "CC_DIR":
                cwd = v
            elif k == "CC_FLAGS":
                flags = v
    except OSError:
        pass
    return cwd, flags


def fresh_target() -> tuple[str, str, str]:
    """`(tmux session, cwd, flags string)` for a chat started from the phone
    with no place named. MEDIA_ASK_TMUX / MEDIA_ASK_CWD / MEDIA_ASK_FLAGS
    override the parts in either layout.

    David's: the amux registration MEDIA_ASK_SESSION names (default scratch),
    in the tmux session amux would put it in. Default: home, in `sasonica`.
    """
    if uses_amux():
        reg = (os.environ.get("MEDIA_ASK_SESSION") or ASK_SESSION_DEFAULT).strip()
        cwd, flags = _amux_registration(reg)
        host = f"amux-{reg}"
    else:
        cwd, flags, host = "", "", CHAT_TMUX
    host = (os.environ.get("MEDIA_ASK_TMUX") or "").strip() or host
    cwd = os.path.expanduser((os.environ.get("MEDIA_ASK_CWD") or "").strip() or cwd or "~")
    flags_s = os.environ.get("MEDIA_ASK_FLAGS")
    return host, cwd, flags_s if flags_s is not None else flags


def place_host(path: str) -> str:
    """The tmux session a chat opened in directory `path` goes into: on
    David's desk the project's (`spl-p-sasonica-app` for
    ~/spl/projects/sasonica/sasonica-app),
    else the folder's name, as tmux-organise-panes would file it."""
    if projects():
        return project_name(path) or os.path.basename(path)
    return (os.environ.get("MEDIA_ASK_TMUX") or "").strip() or CHAT_TMUX


def project_host(project: str) -> str:
    """The tmux session a chat opened in `project` goes into."""
    if projects():
        return project
    return (os.environ.get("MEDIA_ASK_TMUX") or "").strip() or CHAT_TMUX


def revive_host() -> str:
    """Where a resumed or branched session's window opens. "" means the
    tmux session someone is attached to (David's; MEDIA_REPLY_TMUX still
    names it); otherwise a session whose client is held for it."""
    if projects():
        return ""
    return (os.environ.get("MEDIA_REPLY_TMUX") or "").strip() or CHAT_TMUX


def holds_client() -> bool:
    """Whether an app chat's window needs a client held on its tmux session.
    Always: Claude Code's TUI will not start without one. Named so the
    question has one answer, not so it can differ."""
    return True


def expects_move_hook() -> bool:
    """Whether a SessionStart hook will move a new window into its project's
    tmux session (so a window's final session is not where we opened it)."""
    return projects()


def project_of_path(path: str) -> str:
    """What the default layout calls the project a directory is: its basename
    (`<group>/<name>` for one in a group), or the chat session's name for
    home (a basename of `ryer` names nobody)."""
    path = (path or "").rstrip("/")
    if not path or os.path.expanduser("~").rstrip("/") == path:
        return CHAT_TMUX
    rel = project_rel(path)
    if "/" in rel:
        return rel                      # a project in a group: `spl/admin`
    return os.path.basename(path)


def workspace_for(host: str, cwd: str) -> str:
    """The workspace a session is filed and voiced under, given the tmux
    session it opened in (or would have). David's: the tmux session — it is
    the project. Default: the tmux session is `sasonica` for every chat, so
    the folder names the project instead."""
    if projects():
        return host
    if host and host != CHAT_TMUX:
        return host
    return project_of_path(cwd)


def series_name(host: str) -> str:
    """The series a tmux session's conversations are filed under. amux names
    its session `amux-<registration>`, so a phone-started chat in the scratch
    registration ran in `amux-scratch` while a pane opened in ~/scratch ran in
    `scratch`: one folder, two shelves. The registration is the name."""
    host = (host or "").strip()
    if host.startswith("amux-") and len(host) > len("amux-"):
        return host[len("amux-"):]
    return host


def encoded_project_label(tail: str) -> str:
    """The project name for a folder `<tail>` under a projects root:
    `p-<tail>` on David's desk (what his tmux session for it is called),
    `<tail>` elsewhere. A project in a group (`sasonica/sasonica-app`) is
    named by `project_name` (`spl-p-sasonica-app`)."""
    tail = tail.strip().strip("/")
    if not tail:
        return ""
    if "/" in tail:
        group, _, kid = tail.partition("/")
        kid = kid.split("/", 1)[0]
        idx = _index()
        for name, row in idx["projects"].items():
            if row["group"] == group and row["folder"] == kid:
                return name
        return _name(projects_root(), group, kid, idx)
    return f"p-{tail}" if projects() else tail


# --- roots and groups ----------------------------------------------------------------
#
# Projects live in project roots: ~/projects, and any other folder listed in
# the server's project-roots.json (~/spl/projects). A folder in a root that
# holds project folders and is not a project itself (no `.git`) is a group:
# ~/spl/projects/sasonica. Each folder in a group is a project of its own.
# Nothing about groups is configured: the folders say.
#
# Names, David's desk: `p-<folder>` in ~/projects, `<owner>-p-<folder>` in
# another root, grouped or not (`spl-p-sasonica-app` for
# ~/spl/projects/sasonica/sasonica-app), `<prefix><group>-<folder>` only
# when two projects share a folder name. The tmux session has the same name
# (tmux-organise-panes).
# Default layout: `<group>/<folder>`, else the folder's name.
#
# A link in a root or a group (~/projects/sasonica-app, left at an old
# path) is the project it points at, and so is any name it went by
# (`p-sasonica-app`, `p-spl-sasonica-app` from 18 Oct): `canonical_project`.
# David, 18–19 Oct 2026.

#: How long one look at the roots is trusted (it is read per request).
GROUPS_TTL_S = 5.0
_index_seen: tuple[float, tuple, dict] = (0.0, (), {})


def projects_root() -> str:
    """The default projects root: MEDIA_PROJECTS_DIR, else ~/projects."""
    return os.path.expanduser((os.environ.get("MEDIA_PROJECTS_DIR") or "").strip()
                              or "~/projects").rstrip("/")


def projects_roots() -> list[str]:
    """Every projects root, the default first: the server's list
    (`<state>/project-roots.json`, project_roots.py), else the default.
    Folders that are not there are left out."""
    from . import harness_profiles

    raw: list = []
    try:
        data = json.loads((harness_profiles.state_dir() / "project-roots.json").read_text())
        raw = data if isinstance(data, list) else []
    except (OSError, ValueError):
        pass
    paths = [r if isinstance(r, str) else r.get("path") if isinstance(r, dict) else None
             for r in raw]
    out: list[str] = []
    for p in [*(x for x in paths if isinstance(x, str) and x.strip()), projects_root()]:
        p = os.path.expanduser(p).rstrip("/")
        if os.path.isdir(p) and p not in out:
            out.append(p)
    return out


def project_folders() -> list[str]:
    """Single project folders outside every root (~/agent-config, which
    must stay where it is): top-level `project_dirs` in config.toml, or
    MEDIA_PROJECT_DIRS (colon-separated). Each is a project of its own,
    named for its folder, `p-<folder>` on David's desk. Folders that are
    not there are left out (David, 19 Oct 2026)."""
    raw = os.environ.get("MEDIA_PROJECT_DIRS")
    if raw is not None:
        items: list = raw.split(":")
    else:
        items = config.load().get("project_dirs") or []
        items = items if isinstance(items, list) else []
    out: list[str] = []
    for p in items:
        if not isinstance(p, str) or not p.strip():
            continue
        p = os.path.expanduser(p.strip()).rstrip("/")
        if os.path.isdir(p) and p not in out:
            out.append(p)
    return out


def _child_dirs(path: str) -> list[str]:
    """The folders in `path` themselves: not hidden, not links."""
    try:
        with os.scandir(path) as it:
            return sorted(e.name for e in it if not e.name.startswith(".")
                          and e.is_dir(follow_symlinks=False))
    except OSError:
        return []


def _child_links(path: str) -> list[str]:
    try:
        with os.scandir(path) as it:
            return sorted(e.name for e in it if not e.name.startswith(".") and e.is_symlink())
    except OSError:
        return []


def _is_group(path: str) -> bool:
    """No `.git` of its own, and at least one folder in it that has one."""
    if os.path.exists(os.path.join(path, ".git")):
        return False
    return any(os.path.exists(os.path.join(path, k, ".git")) for k in _child_dirs(path))


def _prefix(root: str) -> str:
    """`p-` for the default root; `<owner>-p-` for another, the owner being
    the folder above it (~/spl/projects → `spl-p-`), so a business's
    projects read as its own in tmux and sort together (David, 19 Oct 2026)."""
    if os.path.realpath(root) == os.path.realpath(projects_root()):
        return "p-"
    owner = os.path.basename(os.path.dirname(root.rstrip("/"))).replace(".", "_").replace(" ", "_")
    return f"{owner}-p-" if owner else "p-"


def _name(root: str, group: str, folder: str, idx: dict) -> str:
    """A project's name: `<prefix><folder>` on David's desk
    (`spl-p-sasonica-app`), `<prefix><group>-<folder>` when two projects
    share the folder name; `<group>/<folder>` or `<folder>` elsewhere. A
    folder of its own (`project_folders`) is `p-<folder>` or `<folder>`."""
    if not group and os.path.realpath(os.path.join(root, folder)) in idx.get("single", ()):
        return f"p-{folder}" if projects() else folder
    if not projects():
        return f"{group}/{folder}" if group else folder
    clash = group and idx.get("kids", {}).get(folder, 0) > 1
    return f"{_prefix(root)}{group}-{folder}" if clash else f"{_prefix(root)}{folder}"


def _index() -> dict:
    """One look at the roots: `projects` ({name: {root, rel, group, folder,
    path}}), `groups` ({group: [folder, …]}), `aliases` ({old name: name})
    and `kids` (how many projects have each folder name)."""
    global _index_seen
    roots = projects_roots()
    single = project_folders()
    key = (tuple(roots), tuple(single), projects())
    at, seen_key, idx = _index_seen
    if seen_key == key and time.monotonic() - at < GROUPS_TTL_S:
        return idx
    found: list[tuple[str, str, str]] = []           # (root, group, folder)
    groups: dict[str, list[str]] = {}
    for root in roots:
        for name in _child_dirs(root):
            path = os.path.join(root, name)
            if _is_group(path):
                kids = _child_dirs(path)
                groups.setdefault(name, []).extend(k for k in kids if k not in groups.get(name, []))
                found += [(root, name, k) for k in kids]
            else:
                found.append((root, "", name))
    kids: dict[str, int] = {}
    for _r, _g, k in found:
        kids[k] = kids.get(k, 0) + 1
    idx = {"kids": kids, "groups": groups, "projects": {}, "aliases": {}, "by_path": {},
           "single": set()}
    in_roots = {os.path.realpath(os.path.join(r, g, f)) for r, g, f in found}
    for p in single:                    # a project in a root wins over a listed folder
        if os.path.realpath(p) not in in_roots:
            found.append((os.path.dirname(p), "", os.path.basename(p)))
            idx["single"].add(os.path.realpath(p))
    for root, group, folder in found:
        name = _name(root, group, folder, idx)
        path = os.path.join(root, group, folder) if group else os.path.join(root, folder)
        idx["projects"][name] = {"root": root, "rel": f"{group}/{folder}" if group else folder,
                                 "group": group, "folder": folder, "path": path}
        idx["by_path"][os.path.realpath(path)] = name
        if projects():
            # Names it went by: 18 Oct's p-<group>-<folder>, and p-<folder>
            # from before it moved into another root.
            for old in ([f"p-{group}-{folder}", f"p-{folder}"] if group else [f"p-{folder}"]):
                if old != name:
                    idx["aliases"].setdefault(old, name)
    # Links at old paths, in a root or in a folder of one that is no project
    # (a group, or one left holding only links: ~/projects/spl after the
    # move): the project they point at.
    pre = "p-" if projects() else ""
    for root in roots:
        spots = [(root, "")] + [(os.path.join(root, d), d) for d in _child_dirs(root)
                                if not os.path.exists(os.path.join(root, d, ".git"))]
        for where, group in spots:
            for link in _child_links(where):
                target = idx["by_path"].get(os.path.realpath(os.path.join(where, link)))
                if not target:
                    continue
                for alias in ([f"{pre}{group}-{link}", f"{pre}{link}"] if group and pre
                              else [f"{group}/{link}", link] if group else [f"{pre}{link}"]):
                    if alias != target and alias not in idx["projects"]:
                        idx["aliases"].setdefault(alias, target)
    for name in idx["projects"]:
        idx["aliases"].pop(name, None)      # a real project is never another's old name
    _index_seen = (time.monotonic(), key, idx)
    return idx


def project_groups() -> dict[str, list[str]]:
    """`{group: [project folder, …]}` across the roots."""
    return _index()["groups"]


def _locate(path: str) -> tuple[str, str, str] | None:
    """`(root, group, folder)` of the project `path` is in (a worktree or
    folder inside one counts as it), links followed; None outside the roots
    or for a root or group itself."""
    if not path:
        return None
    idx = _index()
    p = os.path.expanduser(path)
    roots = projects_roots()
    for base in (os.path.realpath(p), os.path.normpath(p)):
        for root in roots:
            for r in (os.path.realpath(root), root):
                if not base.startswith(r + "/"):
                    continue
                parts = base[len(r) + 1:].split("/")
                if parts[0] in idx["groups"] and _is_group(os.path.join(r, parts[0])):
                    return (root, parts[0], parts[1]) if len(parts) > 1 else None
                return root, "", parts[0]
    for top in idx["single"]:
        for base in (os.path.realpath(p), os.path.normpath(p)):
            if base == top or base.startswith(top + "/"):
                return os.path.dirname(top), "", os.path.basename(top)
    return None


def project_rel(path: str) -> str:
    """The project `path` is in, relative to its root: `agent-media`,
    `sasonica/sasonica-app`, or "" outside the roots. Links are followed."""
    hit = _locate(path)
    if not hit:
        return ""
    _root, group, folder = hit
    return f"{group}/{folder}" if group else folder


def project_name(path: str) -> str:
    """The name of the project `path` is in (`spl-p-sasonica-app` on David's
    desk), or "" outside the roots."""
    hit = _locate(path)
    if not hit:
        return ""
    root, group, folder = hit
    return _name(root, group, folder, _index())


def project_parts(project: str) -> tuple[str, str]:
    """`(group, folder)` for a project in a group, else `("", project)`."""
    row = _index()["projects"].get(canonical_project(project))
    if row and row["group"]:
        return row["group"], row["folder"]
    return "", project


def project_dir(project: str) -> str:
    """The folder of a project known by name, else ""."""
    row = _index()["projects"].get(canonical_project(project))
    return row["path"] if row else ""


def canonical_project(project: str) -> str:
    """The name `project` goes by now: a name from before a move (a link
    left at the old path, or 18 Oct's `p-<group>-<folder>`) is the project
    it points at, so old and new threads list together."""
    if not project:
        return project
    return _index()["aliases"].get(project, project)


# --- writing it ------------------------------------------------------------------

_LINE = re.compile(r"^\s*layout\s*=.*$")


def write_setting(value: str, path: Path | None = None, *, replace: bool = False) -> bool:
    """Put `layout = "<value>"` at the top level of config.toml. Whether the
    file changed. An existing setting is left alone unless `replace`."""
    value = _normalise(value)
    if not value:
        raise ValueError(f"layout must be one of {', '.join(LAYOUTS)}")
    p = path or config.config_path()
    try:
        text = p.read_text()
    except FileNotFoundError:
        text = ""
    lines = text.splitlines()
    new = f'layout = "{value}"'
    first_table = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("[")), len(lines))
    for i, ln in enumerate(lines[:first_table]):
        if _LINE.match(ln):
            if not replace or ln.strip() == new:
                return False
            lines[i] = new
            break
    else:
        block = ["# Where sessions open: \"default\" (one tmux session, `sasonica`) or",
                 "# \"projects-per-tmux-session\" (p-<project> sessions, amux, the move hook).",
                 new, ""]
        lines[first_table:first_table] = block
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines).rstrip("\n") + "\n")
    return True
