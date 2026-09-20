"""The slash menu the reply box offers: which commands exist, and what they do.

A command typed from the phone goes into a real terminal, so the menu has to
agree with the terminal's — and a list written out by hand drifts the first
time Claude Code ships a new one. Claude Code says what it has: started
headless, its very first event names every command that session would accept,
including this project's own skills and commands. We read that event and stop
the process before it says anything to a model, so the list costs nothing but
five seconds.

Two things the event leaves out, and how they are filled:

* **Descriptions.** The event is names only. A skill's own file has the
  sentence that says what it is for, so the ones on disk are read and matched
  by name; a command with no file keeps its name alone, which is what the
  terminal menu shows for those too.
* **The terminal-only commands.** `/resume`, `/help`, `/memory` and their kind
  never appear, because headless cannot run them — and from the phone they
  work, because the phone types into the TUI. Those few are listed here, and
  this is the only hand-kept part of the menu.

The list is cached per project directory and thrown away when Claude Code's
version changes, so an upgrade that adds a command is picked up by itself.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from ._paths import state_dir

log = logging.getLogger(__name__)

#: How long a cached menu is trusted. The version check below is what really
#: keeps it fresh; this is for a skill added to a project mid-day.
CACHE_TTL_S = 6 * 3600

#: Commands the TUI has and headless does not, so they are never in the init
#: event. The phone types into the TUI, so they work from the reply box.
#: This is the only list here that is kept by hand.
TERMINAL_ONLY: tuple[tuple[str, str], ...] = (
    ("resume", "Pick an earlier conversation to carry on"),
    ("help", "What the commands are"),
    ("memory", "Edit the memory files"),
    ("status", "Version, account, model and connection"),
    ("permissions", "What Claude may do without asking"),
    ("cost", "What this session has cost"),
    ("export", "Send the conversation out to a file"),
    ("hooks", "The hooks this project runs"),
    ("login", "Sign in to a different account"),
    ("logout", "Sign out"),
    ("exit", "End the session"),
)

#: A last resort only: Claude Code's bundle has the real sentence for nearly
#: every built-in (see `bundle_commands`), and these are what is shown when a
#: command's description is built at runtime and so is not in there to read —
#: `/exit` is one. A command with neither shows its name alone.
BUILTIN_DESCRIPTIONS = {
    "clear": "Start again with an empty conversation",
    "compact": "Summarise the conversation so far to free room",
    "config": "Settings for this session",
    "context": "What is taking up the context window",
    "model": "Change the model",
    "effort": "How hard the model thinks",
    "fast": "Faster output from the same model",
    "init": "Write a CLAUDE.md for this project",
    "mcp": "The MCP servers and their connections",
    "agents": "The subagents this project has",
    "usage": "How much of the plan's allowance is left",
    "rename": "Rename this conversation",
    "recap": "What this session has done so far",
}

#: A command record in Claude Code's own bundle:
#: `name:"exit",aliases:["quit"],...description:"..."`. The bundle is minified
#: and this is not an interface, so everything here is best-effort: a name we
#: already have from the init event, looked up for its own words.
_RECORD = re.compile(
    rb'name:"(?P<name>[a-z][a-z0-9:-]{1,40})"(?P<rest>.{0,400}?)(?=name:"|$)',
    re.DOTALL)
_ALIASES = re.compile(rb'aliases:\[(?P<list>[^\]]{0,120})\]')
_ALIAS = re.compile(rb'"([a-z][a-z0-9:+#-]{0,30})"')
_BUNDLE_DESC = re.compile(rb'description:"(?P<text>[^"\\]{4,300})"')


def _bundle_path() -> Optional[Path]:
    """Claude Code's own executable, or None."""
    import shutil

    found = shutil.which("claude")
    if not found:
        return None
    real = Path(found).resolve()
    return real if real.is_file() else None


def bundle_commands(names: set) -> dict:
    """`{name: {description, aliases}}` read out of Claude Code's bundle.

    The init event gives names alone, but the bundle each name came from has
    the sentence the terminal shows and the aliases it answers to — `/exit
    (quit)`, `/resume (continue)` — and there is nowhere else to get them. It
    is minified and unpromised, so only names the init event already vouched
    for are looked up, and anything unreadable simply is not found: the menu
    then shows what it showed before.
    """
    path = _bundle_path()
    if not path or not names:
        return {}
    try:
        blob = path.read_bytes()
    except OSError as e:  # noqa: BLE001
        log.debug("slash-menu: cannot read %s (%s)", path, e)
        return {}
    wanted = {n.encode() for n in names}
    out: dict = {}
    for record in _RECORD.finditer(blob):
        name = record.group("name")
        if name not in wanted:
            continue
        # A record is written either way round — `name:"config",aliases:[…]`
        # or `{aliases:[…],…name:"config"` — so the look is both sides of it.
        rest = blob[record.start("rest"):record.end("rest")]
        # Only as far back as this record's own opening brace: the previous
        # command's aliases are 80 bytes away and would be read as this one's.
        before = blob[max(0, record.start() - 160):record.start()].rpartition(b"{")[2]
        entry = out.setdefault(name.decode(), {"description": "", "aliases": []})
        aliases = _ALIASES.search(rest) or _ALIASES.search(before)
        if aliases and not entry["aliases"]:
            found = [a.decode() for a in _ALIAS.findall(aliases.group("list"))]
            entry["aliases"] = [a for a in found if a != name.decode()]
        described = _BUNDLE_DESC.search(rest) or _BUNDLE_DESC.search(before)
        if described and not entry["description"]:
            entry["description"] = described.group("text").decode(errors="replace").strip()
    return out


_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_DESC = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)


def _describe(path: Path) -> str:
    """The one-line description in a skill or command file's front matter."""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return ""
    front = _FRONT.match(head)
    if not front:
        return ""
    found = _DESC.search(front.group(1))
    return (found.group(1).strip().strip('"\'') if found else "")


def descriptions(cwd: Optional[Path] = None) -> dict:
    """`{command name: description}` for every skill and command on disk.

    Both the user's own (`~/.claude`) and the project's, with the project's
    winning — which is the order Claude Code itself resolves them in.
    """
    out: dict = {}
    roots = [Path.home() / ".claude"]
    if cwd:
        roots.append(Path(cwd) / ".claude")
    for root in roots:
        for skill in sorted(root.glob("skills/*/SKILL.md")):
            out[skill.parent.name] = _describe(skill)
        for cmd in sorted(root.glob("commands/*.md")):
            out[cmd.stem] = _describe(cmd)
    return out


def _cache_path(cwd: str) -> Path:
    from .book_tracks import safe_name

    return state_dir() / "slash-menu" / f"{safe_name(cwd.strip('/').replace('/', '-'), 80)}.json"


def claude_version() -> str:
    """The installed Claude Code's version, or "" — the cache's stamp."""
    try:
        r = subprocess.run(["claude", "--version"], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (r.stdout or "").strip().split()[0] if r.returncode == 0 else ""


def ask_claude(cwd: str, timeout: float = 60.0) -> tuple[list, str]:
    """`(command names, version)` from Claude Code's own startup event.

    Started headless in `cwd` and stopped as soon as the event arrives, which
    is before the prompt reaches a model: the run is a listing, not a turn.
    """
    try:
        proc = subprocess.Popen(
            ["claude", "-p", "ok", "--output-format", "stream-json",
             "--verbose", "--model", "haiku"],
            cwd=cwd or None, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError as e:  # noqa: BLE001 — no claude here; the caller copes
        log.warning("slash-menu: cannot run claude (%s)", e)
        return [], ""
    deadline = time.time() + timeout
    names: list = []
    version = ""
    try:
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("subtype") == "init":
                names = [str(n) for n in (event.get("slash_commands") or [])]
                version = str(event.get("claude_code_version") or "")
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # noqa: PERF203
            proc.kill()
    return names, version


def build(cwd: str) -> list:
    """The menu for a session in `cwd`: `[{name, description, terminal}]`."""
    names, version = ask_claude(cwd)
    described = descriptions(Path(cwd) if cwd else None)
    offered = [n for n in names if not n.startswith(("__", "mcp__"))]
    # A skill's own file first — it is the one written to be read — then
    # Claude Code's bundle, then the lines kept here.
    bundle = bundle_commands(set(offered) | {n for n, _d in TERMINAL_ONLY})

    def _entry(name: str, fallback: str, terminal: bool) -> dict:
        from_bundle = bundle.get(name) or {}
        return {"name": name,
                "description": (described.get(name.rpartition(":")[2])
                                or from_bundle.get("description")
                                or fallback),
                "aliases": from_bundle.get("aliases") or [],
                "terminal": terminal}

    seen = set(offered)
    menu = [_entry(name, BUILTIN_DESCRIPTIONS.get(name, ""), False) for name in offered]
    menu += [_entry(name, description, True)
             for name, description in TERMINAL_ONLY if name not in seen]
    menu.sort(key=lambda c: c["name"])
    return menu


def menu(cwd: str, *, refresh: bool = False) -> list:
    """`build`, cached per directory until Claude Code's version changes."""
    cwd = str(cwd or os.path.expanduser("~"))
    path = _cache_path(cwd)
    version = claude_version()
    if not refresh:
        try:
            cached = json.loads(path.read_text())
        except (OSError, ValueError):
            cached = None
        if (cached and cached.get("commands")
                and (not version or cached.get("version") == version)
                and time.time() - float(cached.get("at") or 0) < CACHE_TTL_S):
            return cached["commands"]
    commands = build(cwd)
    if not commands:
        return []
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"at": time.time(), "version": version or claude_version(),
             "cwd": cwd, "commands": commands}, indent=1))
    except OSError as e:  # noqa: BLE001 — a menu that cannot be cached is
        log.debug("slash-menu: cannot cache %s (%s)", path, e)
    return commands
