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
* **What is left out.** Claude Code's own built-ins are not offered at all:
  from the phone, `/status` and `/context` draw a screen nobody sees, and
  `/resume` or `/config` open a picker the phone cannot drive, leaving the
  pane to swallow the next reply. The menu is skills and the user's or
  project's own commands — the half that does work and answers.

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


#: Where Claude Code is, when PATH does not say. A systemd service has none
#: of the shell's PATH — the canvas asked `claude` and got nothing, and the
#: menu quietly fell back to the few commands kept here. `MEDIA_CLAUDE_BIN`
#: overrides; otherwise these are looked at in order.
_CLAUDE_GUESSES = (
    "~/.local/share/fnm/aliases/default/bin/claude",
    "~/.local/bin/claude",
    "~/.claude/local/claude",
    "/usr/local/bin/claude",
    "/usr/bin/claude",
)


def claude_bin() -> str:
    """The `claude` to run, or "" — PATH first, then the usual places."""
    import shutil

    override = (os.environ.get("MEDIA_CLAUDE_BIN") or "").strip()
    if override:
        return override if os.path.exists(os.path.expanduser(override)) else ""
    found = shutil.which("claude")
    if found:
        return found
    for guess in _CLAUDE_GUESSES:
        path = os.path.expanduser(guess)
        if os.path.exists(path):
            return path
    return ""


def _bundle_path() -> Optional[Path]:
    """Claude Code's own executable, or None."""
    found = claude_bin()
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
    # Lowest first, each overwriting the last: plugins' and claude.ai's
    # synced skills (`skills/synced/<id>/<name>`) only fill a name nothing
    # nearer has.
    home = Path.home() / ".claude"
    for f in sorted(home.glob("plugins/**/skills/*/SKILL.md")) + sorted(home.glob("skills/synced/*/*/SKILL.md")):
        described = _describe(f)
        if described:
            out[f.parent.name] = described
    for f in sorted(home.glob("plugins/**/commands/*.md")):
        described = _describe(f)
        if described:
            out[f.stem] = described
    roots = [home]
    if cwd:
        roots.append(Path(cwd) / ".claude")
    for root in roots:
        for skill in sorted(root.glob("skills/*/SKILL.md")):
            out[skill.parent.name] = _describe(skill)
        for cmd in sorted(root.glob("commands/*.md")):
            out[cmd.stem] = _describe(cmd)
    return out


#: Claude Code's own skills and commands have no file to read and the bundle
#: does not always give up their words: a line each, for the ones whose job
#: is plain. Only used when nothing else describes them.
BUILTIN_SUMMARIES = {
    "claude-api": "Reference for the Claude API and Anthropic SDKs: models, pricing, parameters, tools.",
    "code-review": "Review the current changes for bugs.",
    "dataviz": "Make charts and dashboards that read well.",
    "fewer-permission-prompts": "Add the commands you keep approving to the allow list.",
    "loop": "Run a prompt or command again and again, on an interval.",
    "run": "Start the project's app and check a change works in it.",
    "schedule": "Create and manage scheduled agents.",
    "simplify": "Tidy the changed code: reuse, simplify, make it efficient.",
    "update-config": "Change Claude Code's settings: permissions, hooks, environment.",
    "workflow-authoring": "Reference for writing multi-agent workflow scripts.",
    "doctor": "Check the Claude Code install for problems.",
}


#: Bumped when a menu entry gains a field, so an older cache is rebuilt.
SCHEMA = 3


def _pack_label(name: str) -> str:
    """`cloudflare-skills` → `Cloudflare`, `anthropic-skills` → `Anthropic`."""
    base = re.sub(r"[-_ ]?skills?$", "", name, flags=re.I) or name
    return " ".join(w.capitalize() for w in re.split(r"[-_ ]+", base) if w)


def groups(cwd: Optional[Path] = None) -> dict:
    """`{command name: group}` for the skills and commands on disk — the
    headings of the phone's slash sheet.

    "This project" for the project's own `.claude`; "Yours" for the user's,
    unless the skill is a link into a skill pack somewhere else on disk (a
    `<pack>/skills/<name>` tree, like the Cloudflare skills), which is
    grouped by the pack's name. The user's own agent-config links count as
    theirs.
    """
    out: dict = {}
    roots = [(Path.home() / ".claude", "Yours")]
    if cwd:
        roots.append((Path(cwd) / ".claude", "This project"))
    for root, label in roots:
        for skill in sorted(root.glob("skills/*/SKILL.md")):
            group = label
            if label == "Yours":
                real = skill.parent.resolve()
                if (real.parent.name == "skills" and "agent-config" not in real.parts
                        and not str(real).startswith(str((Path.home() / ".claude").resolve()))):
                    group = _pack_label(real.parent.parent.name)
            out[skill.parent.name] = group
        for cmd in sorted(root.glob("commands/*.md")):
            out[cmd.stem] = label
    return out


def group_of(name: str, found: dict) -> str:
    """A command's heading: from its file (`groups`), else a plugin's
    `prefix:` (`anthropic-skills:docx` → Anthropic), else Claude Code's own."""
    if name in found:
        return found[name]
    prefix, sep, rest = name.partition(":")
    if sep:
        return found.get(rest) or _pack_label(prefix)
    return "Claude Code"


def _cache_path(cwd: str) -> Path:
    from .book_tracks import safe_name

    return state_dir() / "slash-menu" / f"{safe_name(cwd.strip('/').replace('/', '-'), 80)}.json"


def claude_version() -> str:
    """The installed Claude Code's version, or "" — the cache's stamp."""
    try:
        r = subprocess.run([claude_bin() or "claude", "--version"], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (r.stdout or "").strip().split()[0] if r.returncode == 0 else ""


def ask_claude(cwd: str, timeout: float = 60.0) -> tuple[list, list, str]:
    """`(command names, skill names, version)` from Claude Code's startup event.

    Started headless in `cwd` and stopped as soon as the event arrives, which
    is before the prompt reaches a model: the run is a listing, not a turn.
    """
    try:
        proc = subprocess.Popen(
            [claude_bin() or "claude", "-p", "ok", "--output-format", "stream-json",
             "--verbose", "--model", "haiku"],
            cwd=cwd or None, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError as e:  # noqa: BLE001 — no claude here; the caller copes
        log.warning("slash-menu: cannot run claude (%s)", e)
        return [], [], ""
    deadline = time.time() + timeout
    names: list = []
    skills: list = []
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
                skills = [str(n) for n in (event.get("skills") or [])]
                version = str(event.get("claude_code_version") or "")
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # noqa: PERF203
            proc.kill()
    return names, skills, version


def build(cwd: str) -> list:
    """The menu for a session in `cwd`: `[{name, description, aliases, group}]`.

    Skills and the user's or project's own commands, and nothing else.
    Claude Code's built-ins are left out on purpose: typed from the phone,
    the useful ones draw a screen nobody here can see, and `/resume`,
    `/config` and their kind open a picker in the terminal that the phone
    cannot drive — a pane left sitting in one swallows the next reply. What
    remains is the half worth a menu: the things that do work and answer.
    """
    names, skills, _version = ask_claude(cwd)
    described = descriptions(Path(cwd) if cwd else None)
    found = groups(Path(cwd) if cwd else None)
    offered = [n for n in names
               if not n.startswith(("__", "mcp__"))
               and (n in set(skills) or n.rpartition(":")[2] in described)]
    bundle = bundle_commands(set(offered))
    menu = [{"name": name,
             "description": (described.get(name.rpartition(":")[2])
                             or (bundle.get(name) or {}).get("description", "")
                             or BUILTIN_SUMMARIES.get(name, "")),
             "aliases": (bundle.get(name) or {}).get("aliases") or [],
             "group": group_of(name, found)}
            for name in offered]
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
                and cached.get("schema") == SCHEMA
                and (not version or cached.get("version") == version)
                and time.time() - float(cached.get("at") or 0) < CACHE_TTL_S):
            return cached["commands"]
    commands = build(cwd)
    if not commands:
        return []
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"at": time.time(), "schema": SCHEMA, "version": version or claude_version(),
             "cwd": cwd, "commands": commands}, indent=1))
    except OSError as e:  # noqa: BLE001 — a menu that cannot be cached is
        log.debug("slash-menu: cannot cache %s (%s)", path, e)
    return commands
