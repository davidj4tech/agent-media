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
    """The tmux session a chat opened in directory `path` goes into."""
    if projects():
        return os.path.basename(path)
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
    """What the default layout calls the project a directory is: its basename,
    or the chat session's name for home (a basename of `ryer` names nobody)."""
    path = (path or "").rstrip("/")
    if not path or os.path.expanduser("~").rstrip("/") == path:
        return CHAT_TMUX
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
    """The series for a transcript under ~/projects/<tail> that recorded no
    tmux session: `p-<tail>` on David's desk (what his tmux session for it is
    called), `<tail>` elsewhere."""
    tail = tail.strip()
    if not tail:
        return ""
    return f"p-{tail}" if projects() else tail


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
