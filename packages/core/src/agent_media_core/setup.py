"""media-setup — install hooks and services for agent-media.

Wires the Python intake adapters into Claude Code, Codex, OpenCode,
and (on Termux) the runit service tree. Replaces the manual
settings.json paste in the legacy audio-relay README.

Subcommands:
  media-setup check                     Verify prereq binaries.
  media-setup install-hooks [--dry-run] Merge hook entries into
                                        ~/.claude/settings.json.
  media-setup install-services [--dry-run]
                                        Install services for this host.
                                        Auto-detects runit (Termux /
                                        host-runit) vs systemd --user
                                        (regular Linux); override with
                                        --backend.
  media-setup install-shell [--dry-run] Symlink the tmux popup launcher +
                                        control surface onto PATH
                                        (~/.local/bin, ~/.local/share).
  media-setup status                    Summarize current wiring.

Everything is idempotent. The settings.json writer makes a `.bak` copy
before touching the live file, and only rewrites if the merged content
differs.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Iterable


# --- Hook wiring -----------------------------------------------------------

CLAUDE_HOOK_COMMAND = "media-hook-claude-code"
# Substrings that mark "this entry is OUR hook" (current or legacy).
HOOK_MATCH_SUBSTRINGS = (
    "media-hook-claude-code",
    "claude-code-tts-hook",
)
CLAUDE_HOOK_TIMEOUT = 30
CLAUDE_HOOK_EVENTS = ("Stop", "Notification")


def claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"media-setup: failed to read {path}: {e}") from None


def _merge_hooks(settings: dict, command: str) -> tuple[dict, bool]:
    """Return (new_settings, changed). Idempotent: re-runs leave the
    file untouched if our entries are already current.
    """
    settings = json.loads(json.dumps(settings))  # deep copy
    hooks = settings.setdefault("hooks", {})
    changed = False

    target_entry = {
        "type": "command",
        "command": command,
        "timeout": CLAUDE_HOOK_TIMEOUT,
    }

    for event in CLAUDE_HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        # Look for an existing group containing one of our hooks and
        # rewrite it; otherwise append a new group.
        replaced = False
        for group in groups:
            inner = group.get("hooks") or []
            for i, h in enumerate(inner):
                cmd = (h.get("command") or "")
                if any(s in cmd for s in HOOK_MATCH_SUBSTRINGS):
                    if (h.get("command") != command
                            or h.get("timeout") != CLAUDE_HOOK_TIMEOUT
                            or h.get("type") != "command"):
                        inner[i] = target_entry
                        changed = True
                    replaced = True
                    break
            if replaced:
                break
        if not replaced:
            groups.append({"hooks": [target_entry]})
            changed = True

    return settings, changed


def cmd_install_hooks(args: argparse.Namespace) -> int:
    path = Path(args.settings) if args.settings else claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = _load_json(path)
    merged, changed = _merge_hooks(current, args.command)
    if not changed:
        print(f"media-setup: {path} already up to date")
        return 0
    rendered = json.dumps(merged, indent=2) + "\n"
    if args.dry_run:
        print(f"# would write {path}:")
        print(rendered)
        return 0
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            shutil.copy2(str(path), str(backup))
        except OSError as e:
            raise SystemExit(f"media-setup: backup to {backup} failed: {e}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(rendered)
    tmp.replace(path)
    print(f"media-setup: wrote {path} (backup at {path}.bak)")
    return 0


# --- Env-var migration -----------------------------------------------------

# Per RESTRUCTURE.md. Empty new-name means "drop the variable".
ENV_RENAME = {
    "CLAUDE_TTS_ENGINE":          "MEDIA_RENDER_ENGINE",
    "CLAUDE_TTS_VOICE":           "MEDIA_RENDER_VOICE",
    "CLAUDE_TTS_EDGE_VOICE":      "MEDIA_EDGE_VOICE",
    "CLAUDE_TTS_OPENAI_MODEL":    "MEDIA_OPENAI_MODEL",
    "CLAUDE_TTS_OPENAI_PYTHON":   "MEDIA_OPENAI_PYTHON",
    "CLAUDE_TTS_REALTIME_PYTHON": "MEDIA_REALTIME_PYTHON",
    "CLAUDE_TTS_DROP_DIR":        "MEDIA_DROP_DIR",
    "CLAUDE_TTS_ENABLED":         "MEDIA_ENABLED",
    "CLAUDE_TTS_LONG_THRESHOLD":  "",   # retired (single stream path)
    "AAR_STREAM_HOST":            "MEDIA_STREAM_HOST",
    "AAR_MOPIDY_DUCK_VOLUME":     "MEDIA_DUCK_VOLUME",
    "RELAY_TTS_DROP_BIN":         "",   # retired
    "RELAY_TTS_STREAM_BIN":       "",   # retired
    "RELAY_LOG_FILE":             "MEDIA_LOG_FILE",
    "RELAY_ENV_FILE":             "MEDIA_ENV_FILE",
}


def _rename_in_json_env(env: dict) -> tuple[dict, list[tuple[str, str | None]]]:
    """Apply ENV_RENAME to a flat dict. Returns (new_env, [(old, new), ...]).

    new=None for retired vars (dropped).
    """
    changes: list[tuple[str, str | None]] = []
    out = dict(env)
    for old, new in ENV_RENAME.items():
        if old in out:
            value = out.pop(old)
            if new:
                # Don't clobber a manually-set new-name entry.
                if new not in out:
                    out[new] = value
                changes.append((old, new))
            else:
                changes.append((old, None))
    return out, changes


def cmd_migrate_env(args: argparse.Namespace) -> int:
    """Rename CLAUDE_TTS_*/AAR_*/RELAY_* envs to MEDIA_* in the user's
    settings.json and (if it exists) ~/.config/agent-audio-relay.env.
    """
    paths: list[tuple[str, Path]] = []

    settings_path = Path(args.settings) if args.settings else claude_settings_path()
    if settings_path.exists():
        paths.append(("settings.json", settings_path))

    relay_env = Path.home() / ".config" / "agent-audio-relay.env"
    if relay_env.exists():
        paths.append(("agent-audio-relay.env", relay_env))

    if not paths:
        print("media-setup: nothing to migrate (no settings.json or "
              "agent-audio-relay.env)")
        return 0

    any_changed = False

    for label, path in paths:
        if path.suffix == ".json" or label == "settings.json":
            data = _load_json(path)
            env = data.get("env") or {}
            new_env, changes = _rename_in_json_env(env)
            if not changes:
                print(f"  {label}: nothing to change")
                continue
            for old, new in changes:
                arrow = f"-> {new}" if new else "-> (dropped)"
                print(f"  {label}: {old} {arrow}")
            if not args.dry_run:
                if path.exists():
                    backup = path.with_suffix(path.suffix + ".bak")
                    shutil.copy2(str(path), str(backup))
                data["env"] = new_env
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(json.dumps(data, indent=2) + "\n")
                tmp.replace(path)
                print(f"  {label}: wrote {path} (backup at {backup})")
            any_changed = True
            continue

        # Shell-style env file: simple line-by-line `KEY=VALUE` or
        # `export KEY=VALUE`. Lines we don't recognize are preserved
        # verbatim.
        new_lines: list[str] = []
        changes: list[tuple[str, str | None]] = []
        for raw in path.read_text().splitlines(keepends=True):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(raw)
                continue
            line = stripped
            prefix = ""
            if line.startswith("export "):
                prefix = "export "
                line = line[len("export "):]
            if "=" not in line:
                new_lines.append(raw)
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k in ENV_RENAME:
                new = ENV_RENAME[k]
                if new:
                    new_lines.append(f"{prefix}{new}={v}\n")
                    changes.append((k, new))
                else:
                    changes.append((k, None))
                continue
            new_lines.append(raw)
        if not changes:
            print(f"  {label}: nothing to change")
            continue
        for old, new in changes:
            arrow = f"-> {new}" if new else "-> (dropped)"
            print(f"  {label}: {old} {arrow}")
        if not args.dry_run:
            backup = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(str(path), str(backup))
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text("".join(new_lines))
            tmp.replace(path)
            print(f"  {label}: wrote {path} (backup at {backup})")
        any_changed = True

    if args.dry_run:
        print("\n(dry-run — no files written)")
    return 0


# --- Service wiring (Termux runit + systemd --user) ------------------------

def _service_backend(explicit: str | None) -> str:
    """Pick the service supervisor for this host: 'runit' or 'systemd'.

    'auto' (the default) prefers runit whenever a runit service root is
    present (Termux, or host-runit at /etc/service), since those hosts
    are supervised by runsvdir; otherwise falls back to systemd --user
    when `systemctl` is available.
    """
    if explicit and explicit != "auto":
        return explicit
    if services_dir() is not None:
        return "runit"
    if shutil.which("systemctl") is not None:
        return "systemd"
    return "runit"  # last resort; cmd will surface the missing root


def services_dir() -> Path | None:
    """Where runit looks for services on this host, or None when we
    can't infer (e.g. non-Termux Linux with systemd).

    Detection order:
      1. $PREFIX (Termux-native shells)
      2. /data/data/com.termux/files/usr/var/service exists (Termux,
         even when invoked from inside a proot where $PREFIX isn't set)
      3. /etc/service (host-runit on regular Linux)
    """
    prefix = os.environ.get("PREFIX")
    if prefix and prefix.startswith("/data/data/com.termux"):
        return Path(prefix) / "var" / "service"
    termux_sv = Path("/data/data/com.termux/files/usr/var/service")
    if termux_sv.is_dir():
        return termux_sv
    candidate = Path("/etc/service")
    return candidate if candidate.is_dir() else None


def service_templates_dir() -> Path:
    """Repo-shipped templates under packages/core/services/."""
    return Path(__file__).resolve().parent.parent.parent / "services"


def tmux_dir() -> Path:
    """Repo-shipped tmux integration under packages/core/tmux/."""
    return Path(__file__).resolve().parent.parent.parent / "tmux"


def local_bin() -> Path:
    return Path.home() / ".local" / "bin"


def media_share_dir() -> Path:
    return Path.home() / ".local" / "share" / "agent-media"


def _symlink_into(src: Path, dest: Path, *, dry_run: bool) -> bool:
    """Idempotently symlink ``dest -> src``.

    Replaces a stale symlink of ours, and auto-converts a stale *real file*
    (typically a copy from an older install) into a symlink: dropped if it
    matches ``src``, else backed up first so no local edit is lost. A real
    directory or anything else unexpected is left alone.
    """
    if dest.is_symlink():
        if dest.resolve() == src.resolve():
            print(f"media-setup: {dest.name} already linked")
            return True
        if dry_run:
            print(f"# would relink {dest} -> {src}")
            return True
        dest.unlink()
    elif dest.is_file():
        if dry_run:
            print(f"# would convert real file {dest} -> symlink {src}")
            return True
        if dest.read_bytes() == src.read_bytes():
            dest.unlink()
            print(f"media-setup: {dest.name}: replaced identical real file "
                  f"with symlink")
        else:
            backup = _backup_aside(dest, "shell-backups", dest.name)
            print(f"media-setup: {dest.name}: real file differed from source; "
                  f"backed up to {backup} before relinking", file=sys.stderr)
    elif dest.exists():
        # A real directory (or other non-file) at the link path — not ours.
        print(f"media-setup: {dest} exists and is not our symlink; leaving it",
              file=sys.stderr)
        return False
    if dry_run:
        print(f"# would symlink {dest} -> {src}")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(src)
    print(f"media-setup: linked {dest} -> {src}")
    return True


def _service_dir_matches_template(dest: Path, src: Path) -> bool:
    """True if every file shipped in the template ``src`` exists in ``dest``
    with identical content.

    Extra files in ``dest`` are ignored — a live runit dir carries runtime
    state (``supervise/``, ``log/`` output) that the template never has. We
    only care that nothing the repo manages was hand-edited locally.
    """
    for tpl in src.rglob("*"):
        if not tpl.is_file():
            continue
        live = dest / tpl.relative_to(src)
        if not live.is_file() or live.read_bytes() != tpl.read_bytes():
            return False
    return True


def _backup_aside(path: Path, category: str, name: str) -> Path:
    """Move ``path`` into ``media_share_dir()/category/name`` before we replace
    it with a symlink, so nothing local is lost. The backup lands OUTSIDE both
    the service root (runsvdir scans ``root/*`` and would otherwise supervise a
    backed-up service) and ~/.local/bin (so a backed-up script isn't on PATH).
    A numeric suffix is appended if the target already exists.
    """
    backups = media_share_dir() / category
    backups.mkdir(parents=True, exist_ok=True)
    dest = backups / name
    n = 1
    while dest.exists():
        dest = backups / f"{name}.{n}"
        n += 1
    shutil.move(str(path), str(dest))
    return dest


def _install_one_service(name: str, *, dry_run: bool,
                         root: Path) -> bool:
    """Symlink/copy the template tree into the runit service root.

    Termux's runsvdir scans `service_dir/*` for `run` files. We use
    symlinks so a `git pull` on the repo picks up service edits without
    a re-install.

    A pre-existing *real* directory (typically a stale copy-based install)
    is auto-converted to a symlink: if its tracked files match the template
    we just drop it, otherwise we back it up first so no local edit is lost.
    """
    src = service_templates_dir() / name
    if not src.is_dir():
        print(f"media-setup: template missing: {src}", file=sys.stderr)
        return False
    dest = root / name
    if dest.is_symlink():
        if dest.resolve() == src.resolve():
            print(f"media-setup: {name} already installed")
            return True
        # Our symlink, but pointing elsewhere (e.g. an old repo path): relink.
        if dry_run:
            print(f"# would relink {dest} -> {src}")
            return True
        dest.unlink()
    elif dest.is_dir():
        # A real directory. Convert it to a symlink iff it's recognizably one
        # of our service dirs (has a `run` script); never touch a stranger.
        if not (dest / "run").is_file():
            print(f"media-setup: {dest} exists and is not a service dir; "
                  f"leaving it", file=sys.stderr)
            return False
        if dry_run:
            print(f"# would convert real dir {dest} -> symlink {src}")
            return True
        if _service_dir_matches_template(dest, src):
            shutil.rmtree(dest)
            print(f"media-setup: {name}: replaced identical real dir "
                  f"with symlink")
        else:
            backup = _backup_aside(dest, "service-backups", name)
            print(f"media-setup: {name}: real dir differed from template; "
                  f"backed up to {backup} before relinking", file=sys.stderr)
    elif dest.exists():
        # A real non-directory file sitting at the service path — not ours.
        print(f"media-setup: {dest} exists and is not our symlink; leaving it",
              file=sys.stderr)
        return False
    elif dry_run:
        print(f"# would symlink {dest} -> {src}")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(src)
    print(f"media-setup: installed {dest} -> {src}")
    return True


# systemd --user backend. We don't translate the run scripts into native
# ExecStart lines — we point ExecStart at the very same `run` script so
# the mpv flags / MCP bind logic stay in one place across both backends.
# The shebang (Termux sh) is bypassed by invoking via `/bin/sh`.

SYSTEMD_UNIT_TEMPLATE = """\
[Unit]
Description=agent-media {name}
PartOf=default.target

[Service]
Type=simple
EnvironmentFile=-%h/.config/agent-media.env
Environment=PATH={bindir}:/usr/local/bin:/usr/bin:/bin
ExecStart=/bin/sh {runscript}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
"""


def systemd_user_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "systemd" / "user"


def _entrypoint_bindir() -> Path:
    """Console-script dir of this install — so a venv install's
    `media-mcp-http` resolves on the unit's PATH without the user having
    it on their login PATH. Uses sysconfig (not `sys.executable`, whose
    venv symlink resolves back to the base interpreter's bin).
    """
    return Path(sysconfig.get_path("scripts"))


def _systemd_unit_name(name: str) -> str:
    """Map a template dir name to a namespaced unit file name.

    Collapses a redundant leading `media-` so `media-mcp` becomes
    `agent-media-mcp.service`, while `sink-speech` becomes
    `agent-media-sink-speech.service`.
    """
    stem = name[len("media-"):] if name.startswith("media-") else name
    return f"agent-media-{stem}.service"


def _systemctl_user(*argv: str) -> int:
    return subprocess.call(["systemctl", "--user", *argv])


def _install_one_systemd(name: str, *, dry_run: bool, root: Path) -> str | None:
    """Write a systemd --user unit for `name`. Returns the unit file
    name on success (so the caller can enable it), or None on failure.
    """
    src = service_templates_dir() / name
    run = src / "run"
    if not run.is_file():
        print(f"media-setup: template missing: {run}", file=sys.stderr)
        return None
    unit = _systemd_unit_name(name)
    dest = root / unit
    content = SYSTEMD_UNIT_TEMPLATE.format(
        name=name, bindir=_entrypoint_bindir(), runscript=run)
    if dry_run:
        print(f"# would write {dest}:")
        print(content)
        return unit
    if dest.exists() and dest.read_text() == content:
        print(f"media-setup: {unit} already up to date")
        return unit
    root.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    print(f"media-setup: wrote {dest}")
    return unit


def _install_services_systemd(args: argparse.Namespace,
                              names: list[str]) -> int:
    root = systemd_user_dir()
    units: list[str] = []
    ok = True
    for name in names:
        unit = _install_one_systemd(name, dry_run=args.dry_run, root=root)
        if unit is None:
            ok = False
        else:
            units.append(unit)
    if args.dry_run:
        if units and args.now:
            print(f"# would: systemctl --user daemon-reload && enable --now "
                  f"{' '.join(units)}")
        return 0 if ok else 1
    if units:
        _systemctl_user("daemon-reload")
        if args.now:
            ok = _systemctl_user("enable", "--now", *units) == 0 and ok
        else:
            print("media-setup: units written. Start them with:\n"
                  f"  systemctl --user enable --now {' '.join(units)}")
    return 0 if ok else 1


def cmd_install_services(args: argparse.Namespace) -> int:
    templates = service_templates_dir()
    if not templates.is_dir():
        print(f"media-setup: service templates not found at {templates}",
              file=sys.stderr)
        return 1
    names = args.services or [p.name for p in templates.iterdir() if p.is_dir()]

    backend = _service_backend(getattr(args, "backend", None))
    if backend == "systemd":
        return _install_services_systemd(args, names)

    # runit
    root = Path(args.root) if args.root else services_dir()
    if root is None:
        print("media-setup: no runit service root inferred — pass --root, "
              "or use --backend systemd", file=sys.stderr)
        return 2
    ok = True
    for name in names:
        ok = _install_one_service(name, dry_run=args.dry_run, root=root) and ok
    return 0 if ok else 1


# --- Rooms audio hub (server role) -----------------------------------------
# `media-setup server` wires a PipeWire/systemd host as a Snapcast render hub:
# null sinks (am[/am-music]) -> parec -> /tmp/snapfifo-<sink> -> snapserver.
# This is the USER-level half (sinks + parec bridge + rooms env). snapserver
# itself needs root (pkg + /etc/snapserver.conf + a same-user override + the
# tmpfiles FIFO pre-create), so those are printed for sudo / an ansible
# audio_server role. PipeWire/systemd hosts only — Termux keeps the openal AO
# default (it survives BT route changes), so this never runs there.

ROOMS_SPEECH_SINK = "am"
ROOMS_MUSIC_SINK = "am-music"

_AM_SINKS_UNIT = """\
[Unit]
Description=agent-media: PipeWire null sinks for rooms audio
After=pipewire.service pipewire-pulse.service wireplumber.service
Requires=pipewire.service

[Service]
Type=oneshot
RemainAfterExit=yes
{execstarts}

[Install]
WantedBy=default.target
"""

_AM_SNAPFIFO_UNIT = """\
[Unit]
Description=agent-media: parec %i.monitor -> /tmp/snapfifo-%i (snapserver pipe)
After=am-sinks.service
Requires=am-sinks.service

[Service]
ExecStart=/bin/sh -c "exec parec --device=%i.monitor --rate=48000 --format=s16le --channels=2 > /tmp/snapfifo-%i"
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
"""

# PipeWire host: per-clip `audio-device=pulse/<sink>` routing needs the pulse
# AO. (openal — the default that survives Termux BT route changes — does not
# understand pulse device ids.)
_ROOMS_ENV_DEFAULTS = (
    ("MEDIA_SPEECH_DEFAULT_TARGET", "rooms"),
    ("MEDIA_ROOMS_SINK", ROOMS_SPEECH_SINK),
    ("MEDIA_SPEECH_AO", "pulse"),
    ("MEDIA_RENDER_ENGINE", "edge"),
)


def _agent_media_env_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "agent-media.env"


def _merge_env_defaults(env_path: Path, defaults, *, dry_run: bool) -> list[str]:
    """Append missing KEY=value defaults to agent-media.env. Never overwrites a
    key the user already set. Returns the keys added."""
    existing = env_path.read_text() if env_path.exists() else ""
    present = {ln.split("=", 1)[0].strip()
               for ln in existing.splitlines()
               if "=" in ln and not ln.lstrip().startswith("#")}
    add = [(k, v) for k, v in defaults if k not in present]
    if not add:
        return []
    block = "".join(f"{k}={v}\n" for k, v in add)
    if dry_run:
        print(f"# would append to {env_path}:\n{block}", end="")
        return [k for k, _ in add]
    env_path.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    with env_path.open("a") as fh:
        fh.write(sep + ("" if existing else "# agent-media host config\n") + block)
    return [k for k, _ in add]


def cmd_server(args: argparse.Namespace) -> int:
    """Wire this host as a Snapcast rooms render hub (PipeWire/systemd only)."""
    if _service_backend(getattr(args, "backend", None) or "auto") != "systemd":
        print("media-setup server: PipeWire/systemd --user hosts only "
              "(Termux/runit hosts are snapclients, not the hub).",
              file=sys.stderr)
        return 1

    sinks = [ROOMS_SPEECH_SINK] + ([ROOMS_MUSIC_SINK] if args.music else [])
    root = systemd_user_dir()
    execstarts = "\n".join(
        f'ExecStart=/bin/sh -c "pactl list short sinks | cut -f2 | grep -qx {s} '
        f'|| pactl load-module module-null-sink sink_name={s} '
        f'sink_properties=device.description={s}"'
        for s in sinks)
    for name, content in (("am-sinks.service", _AM_SINKS_UNIT.format(execstarts=execstarts)),
                          ("am-snapfifo@.service", _AM_SNAPFIFO_UNIT)):
        dest = root / name
        if args.dry_run:
            print(f"# would write {dest}:\n{content}")
        elif dest.exists() and dest.read_text() == content:
            print(f"media-setup: {name} already up to date")
        else:
            root.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
            print(f"media-setup: wrote {dest}")
    units = ["am-sinks.service"] + [f"am-snapfifo@{s}.service" for s in sinks]

    added = _merge_env_defaults(_agent_media_env_path(), _ROOMS_ENV_DEFAULTS,
                                dry_run=args.dry_run)
    print(f"media-setup: set {', '.join(added)} in agent-media.env" if added
          else "media-setup: agent-media.env already has the rooms env")

    if args.dry_run:
        print(f"# would: systemctl --user daemon-reload && enable --now {' '.join(units)}")
    else:
        _systemctl_user("daemon-reload")
        if args.now:
            _systemctl_user("enable", "--now", *units)
        else:
            print("media-setup: units written. Start them with:\n"
                  f"  systemctl --user enable --now {' '.join(units)}")

    user = os.environ.get("USER") or Path.home().name
    src_lines = "; ".join(
        f"source = pipe:///tmp/snapfifo-{s}?name={s}&codec=pcm&sampleformat=48000:16:2"
        for s in sinks)
    print("\nmedia-setup: snapserver itself needs root — run the dotfiles "
          "audio_server ansible role, or as root:\n"
          f"  * /etc/snapserver.conf [stream]: {src_lines}\n"
          f"  * snapserver.service override -> User={user} Group={user} "
          "(same-user FIFO constraint)\n"
          f"  * /etc/tmpfiles.d/snapfifo.conf: pre-create "
          f"{', '.join('/tmp/snapfifo-'+s for s in sinks)} owned by {user}\n"
          f"  * loginctl enable-linger {user}; systemctl enable --now snapserver",
          file=sys.stderr)
    return 0


# --- Shell integration (tmux popup + control surface) ----------------------

def cmd_install_shell(args: argparse.Namespace) -> int:
    """Symlink the shell-facing bits onto PATH so the `prefix a` popup works:
    the executable helpers in tmux/ (media-popup, media-popup-open, …) into
    ~/.local/bin, and the tmux control surface (media.tmux) into
    ~/.local/share/agent-media/. Repo-source symlinks, so a `git pull` keeps
    them current; the bin loop enumerates tmux/, so new helper scripts are
    picked up automatically."""
    src_dir = tmux_dir()
    if not src_dir.is_dir():
        print(f"media-setup: tmux dir not found at {src_dir}", file=sys.stderr)
        return 1
    ok = True
    bindir = local_bin()
    for f in sorted(src_dir.iterdir()):
        # Executable scripts (not the .tmux source) → ~/.local/bin.
        if f.is_file() and f.suffix != ".tmux" and os.access(f, os.X_OK):
            ok = _symlink_into(f, bindir / f.name, dry_run=args.dry_run) and ok
    # The tmux control surface, sourced from tmux.conf.local behind an
    # if-shell guard → ~/.local/share/agent-media/.
    tmux_conf = src_dir / "media.tmux"
    if tmux_conf.is_file():
        ok = _symlink_into(tmux_conf, media_share_dir() / "media.tmux",
                           dry_run=args.dry_run) and ok
    return 0 if ok else 1


# --- Prereq check ----------------------------------------------------------

PREREQS: tuple[tuple[str, str], ...] = (
    ("python3", "python (>= 3.11)"),
    ("mpv",     "mpv (used by sink-speech)"),
    ("mpc",     "mpd client (sink-music helper)"),
    ("edge-tts", "edge-tts (default render engine)"),
    ("jq",      "jq (legacy hook helpers; can drop after full retire)"),
)


def cmd_check(_: argparse.Namespace) -> int:
    missing = []
    for bin_, label in PREREQS:
        if shutil.which(bin_) is None:
            missing.append((bin_, label))
            print(f"  MISSING  {bin_:12} ({label})")
        else:
            print(f"  ok       {bin_}")
    return 0 if not missing else 1


# --- Status ----------------------------------------------------------------

def cmd_status(_: argparse.Namespace) -> int:
    path = claude_settings_path()
    if not path.exists():
        print(f"settings: {path} missing")
    else:
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            print(f"settings: {path} unparseable: {e}")
            return 1
        for event in CLAUDE_HOOK_EVENTS:
            groups = (data.get("hooks") or {}).get(event) or []
            cmds = [h.get("command") for g in groups for h in (g.get("hooks") or [])]
            print(f"hook {event}: {cmds or '(none)'}")
    templates = service_templates_dir()
    names = ([p.name for p in sorted(templates.iterdir()) if p.is_dir()]
             if templates.is_dir() else [])
    backend = _service_backend(None)
    print(f"service backend: {backend}")
    if backend == "runit":
        root = services_dir()
        for name in names:
            link = (root / name) if root else None
            mark = ("installed" if link and (link.exists() or link.is_symlink())
                    else "MISSING")
            print(f"service {name}: {mark}")
    else:  # systemd
        sd = systemd_user_dir()
        for name in names:
            unit = _systemd_unit_name(name)
            if not (sd / unit).exists():
                print(f"service {unit}: MISSING")
                continue
            active = subprocess.run(
                ["systemctl", "--user", "is-active", unit],
                capture_output=True, text=True).stdout.strip() or "unknown"
            print(f"service {unit}: installed ({active})")
    return 0


# --- CLI -------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="media-setup",
                                 description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("check", help="Verify prereq binaries")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("install-hooks",
                        help="Merge hook entries into ~/.claude/settings.json")
    sp.add_argument("--settings", help="Path to settings.json (default: "
                    "~/.claude/settings.json)")
    sp.add_argument("--command", default=CLAUDE_HOOK_COMMAND,
                    help="Hook command name to register")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_install_hooks)

    sp = sub.add_parser("install-services",
                        help="Install services (runit on Termux, systemd "
                             "--user on regular Linux)")
    sp.add_argument("--backend", choices=("auto", "runit", "systemd"),
                    default="auto",
                    help="Service supervisor (default: auto-detect)")
    sp.add_argument("--root", help="runit service root (default: "
                    "$PREFIX/var/service on Termux; ignored for systemd)")
    sp.add_argument("--now", action="store_true",
                    help="systemd: enable --now the units after writing")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("services", nargs="*",
                    help="Specific service names (default: all in repo)")
    sp.set_defaults(func=cmd_install_services)

    sp = sub.add_parser("server",
                        help="Wire this host as a Snapcast rooms render hub "
                             "(PipeWire null sinks + parec->FIFO + rooms env; "
                             "PipeWire/systemd hosts only)")
    sp.add_argument("--music", action="store_true",
                    help="also wire the am-music sink/bridge (default: am only)")
    sp.add_argument("--now", action="store_true",
                    help="enable --now the units after writing")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_server)

    sp = sub.add_parser("install-shell",
                        help="Symlink the tmux popup launcher + control "
                             "surface onto PATH (~/.local/bin, "
                             "~/.local/share/agent-media)")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_install_shell)

    sp = sub.add_parser("migrate-env",
                        help="Rename CLAUDE_TTS_*/AAR_*/RELAY_* envs to "
                             "MEDIA_* in settings.json + agent-audio-relay.env")
    sp.add_argument("--settings", help="Path to settings.json (default: "
                    "~/.claude/settings.json)")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_migrate_env)

    sp = sub.add_parser("status",
                        help="Show current wiring")
    sp.set_defaults(func=cmd_status)

    return p


def main() -> int:
    args = _build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
