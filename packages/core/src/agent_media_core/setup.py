"""media-setup — install hooks and services for agent-media.

Wires the Python intake adapters into Claude Code, Codex, OpenCode,
and (on Termux) the runit service tree. Replaces the manual
settings.json paste in the legacy audio-relay README.

Subcommands:
  media-setup check                     Verify prereq binaries.
  media-setup install-hooks [--dry-run] Merge hook entries into
                                        ~/.claude/settings.json.
  media-setup install-services [--dry-run]
                                        Install runit services on
                                        Termux (or print what would).
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


# --- Service wiring (Termux runit) -----------------------------------------

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


def _install_one_service(name: str, *, dry_run: bool,
                         root: Path) -> bool:
    """Symlink/copy the template tree into the runit service root.

    Termux's runsvdir scans `service_dir/*` for `run` files. We use
    symlinks so a `git pull` on the repo picks up service edits without
    a re-install.
    """
    src = service_templates_dir() / name
    if not src.is_dir():
        print(f"media-setup: template missing: {src}", file=sys.stderr)
        return False
    dest = root / name
    if dest.exists() or dest.is_symlink():
        # Already in place; only complain if it points elsewhere.
        current = (dest.resolve()
                   if dest.is_symlink() or dest.is_dir() else None)
        if current and current.resolve() != src.resolve():
            print(f"media-setup: {dest} already exists and differs "
                  f"from {src}", file=sys.stderr)
            return False
        print(f"media-setup: {name} already installed")
        return True
    if dry_run:
        print(f"# would symlink {dest} -> {src}")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(src)
    print(f"media-setup: installed {dest} -> {src}")
    return True


def cmd_install_services(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else services_dir()
    if root is None:
        print("media-setup: no service root inferred — pass --root or "
              "install systemd units instead", file=sys.stderr)
        return 2
    templates = service_templates_dir()
    if not templates.is_dir():
        print(f"media-setup: service templates not found at {templates}",
              file=sys.stderr)
        return 1
    names = args.services or [p.name for p in templates.iterdir() if p.is_dir()]
    ok = True
    for name in names:
        ok = _install_one_service(name, dry_run=args.dry_run, root=root) and ok
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
    root = services_dir()
    if root and root.is_dir():
        templates = service_templates_dir()
        for tpl in (sorted(templates.iterdir()) if templates.is_dir() else []):
            if not tpl.is_dir():
                continue
            link = root / tpl.name
            mark = "installed" if (link.exists() or link.is_symlink()) else "MISSING"
            print(f"service {tpl.name}: {mark}")
    else:
        print("service root: not detected (non-Termux?)")
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
                        help="Install runit services on Termux")
    sp.add_argument("--root", help="Service root (default: $PREFIX/var/service "
                    "on Termux)")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("services", nargs="*",
                    help="Specific service names (default: all in repo)")
    sp.set_defaults(func=cmd_install_services)

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
