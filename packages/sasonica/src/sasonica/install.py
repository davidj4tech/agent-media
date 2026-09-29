"""`sasonica install` — this computer as a Sasonica server, from the binary.

What deploy/android/install.sh does inside the phone's Debian, for a host that
has the binary instead of a checkout:

  1. a shim in ~/.local/bin for every command (`media`, `media-hook-claude-code`,
     `edge-tts`…), two lines that exec the binary with the command's name, so
     Claude Code's hooks, opencode's plugin and the run scripts find the names
     they already use. A file there that is not one of these shims (a checkout's
     venv link) is left alone unless --force;
  2. this host's config when it has none: role `origin`, headless sessions on;
  3. the agents' hooks: Claude Code's settings, opencode's plugin, for those
     installed;
  4. two services, sasonica-canvas (`sasonica serve`) and sasonica-sessiond
     (`sasonica sessiond`): systemd --user units on Linux, launchd agents on
     a Mac (~/Library/LaunchAgents/com.sasonica.*.plist, logs in
     ~/Library/Logs/sasonica). Not when the host already runs a canvas from a
     checkout (agent-media-visual-canvas.service), unless --force.

Safe to run again, and run again after replacing the binary: shims point at
the binary's path, not at a version.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from sasonica.__main__ import binary_path, commands

SHIM_MARK = "# sasonica shim"
UNITS = {
    "sasonica-canvas": ("serve", "Sasonica server: the app's API and the canvas"),
    "sasonica-sessiond": ("sessiond", "Sasonica session holder: headless agent sessions"),
}
#: A checkout's canvas unit (packages/visual/systemd): a host with it already
#: serves 8781.
CHECKOUT_CANVAS_UNIT = "agent-media-visual-canvas.service"


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def shim_text(binary: str, name: str) -> str:
    return f'#!/bin/sh\n{SHIM_MARK}\nexec "{binary}" {name} "$@"\n'


def is_shim(path: Path) -> bool:
    try:
        return not path.is_symlink() and SHIM_MARK in path.read_text(errors="replace")[:200]
    except OSError:
        return False


def write_shims(binary: str, bin_dir: Path, *, force: bool, dry_run: bool) -> tuple[list, list]:
    """A shim per command. Returns (written, kept) names."""
    written, kept = [], []
    if not dry_run:
        bin_dir.mkdir(parents=True, exist_ok=True)
    for name in sorted(commands()):
        dest = bin_dir / name
        if (dest.exists() or dest.is_symlink()) and not is_shim(dest) and not force:
            kept.append(name)
            continue
        written.append(name)
        if dry_run:
            continue
        if dest.is_symlink() or dest.exists():
            dest.unlink()
        dest.write_text(shim_text(binary, name))
        dest.chmod(0o755)
    return written, kept


def unit_text(binary: str, word: str, what: str, bind: str, port: int) -> str:
    args = f" --bind {bind} --port {port}" if word == "serve" else ""
    return f"""[Unit]
Description={what}
After=network-online.target

[Service]
# Written by `sasonica install`. The shims and the agents' own installers
# (~/.local/bin, ~/.opencode/bin) on PATH: the session holder starts the
# agents by name, and their hooks call the shims.
Environment=PATH=%h/.local/bin:%h/.opencode/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=-%h/.config/agent-media.env
ExecStart="{binary}" {word}{args}
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
"""


#: launchd's label for each service (a Mac).
LABELS = {name: "com.sasonica." + name.removeprefix("sasonica-") for name in UNITS}


def launch_agents() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_text(label: str, binary: str, word: str, bind: str, port: int) -> str:
    """A launchd agent that runs `sasonica <word>` at login and keeps it up.
    launchd has no EnvironmentFile: the server reads ~/.config/agent-media.env
    itself (load_env_file); PATH is set here, as the systemd unit does."""
    import plistlib

    args = [binary, word] + (["--bind", bind, "--port", str(port)] if word == "serve" else [])
    home = Path.home()
    logs = home / "Library" / "Logs" / "sasonica"
    path = ":".join([str(home / ".local" / "bin"), str(home / ".opencode" / "bin"),
                     "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"])
    return plistlib.dumps({
        "Label": label, "ProgramArguments": args, "RunAtLoad": True, "KeepAlive": True,
        "EnvironmentVariables": {"PATH": path},
        "WorkingDirectory": str(home),
        "StandardOutPath": str(logs / f"{label}.log"),
        "StandardErrorPath": str(logs / f"{label}.log"),
    }).decode()


def _launchd(binary: str, a) -> int:
    """The two services as launchd agents in the login session (`gui/<uid>`),
    replaced if they are there already, so a new binary starts."""
    domain = f"gui/{os.getuid()}"
    agents = launch_agents()
    rc = 0
    for name, (word, _what) in UNITS.items():
        label = LABELS[name]
        plist = agents / f"{label}.plist"
        print(f"  {plist}")
        if not a.dry_run:
            agents.mkdir(parents=True, exist_ok=True)
            (Path.home() / "Library" / "Logs" / "sasonica").mkdir(parents=True, exist_ok=True)
            plist.write_text(plist_text(label, binary, word, a.bind, a.port))
        # bootout fails when it is not loaded; that is fine.
        if not a.dry_run:
            subprocess.run(["launchctl", "bootout", f"{domain}/{label}"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rc = _run(["launchctl", "bootstrap", domain, str(plist)], dry_run=a.dry_run) or rc
    print("  (they start at login; logs in ~/Library/Logs/sasonica)")
    return rc


def _run(argv: list[str], *, dry_run: bool) -> int:
    print("  $ " + " ".join(argv))
    if dry_run:
        return 0
    return subprocess.run(argv).returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sasonica install", description=__doc__.split("\n\n")[0])
    ap.add_argument("--binary", help="the binary the shims and services run "
                    "(default: the one this runs under)")
    ap.add_argument("--bin-dir", type=Path, default=Path.home() / ".local" / "bin")
    ap.add_argument("--bind", default="0.0.0.0",
                    help="the address the server listens on (default 0.0.0.0: "
                         "the phone reaches it across the network)")
    ap.add_argument("--port", type=int, default=8781)
    ap.add_argument("--no-services", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="replace commands and services a checkout install put there")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    # Its lines in order with the commands it runs, when piped (curl | bash).
    sys.stdout.reconfigure(line_buffering=True)

    binary = a.binary or binary_path()
    if not binary:
        print("sasonica install: not running from the binary; a checkout installs "
              "with pip and media-setup (README)", file=sys.stderr)
        return 2
    binary = str(Path(binary).resolve())
    me = [binary]

    print("== Commands")
    written, kept = write_shims(binary, a.bin_dir, force=a.force, dry_run=a.dry_run)
    print(f"  {len(written)} in {a.bin_dir}")
    if kept:
        print(f"  kept {len(kept)} that are not sasonica's (a checkout's?): "
              f"{', '.join(kept[:6])}{'…' if len(kept) > 6 else ''} — --force replaces them")
    if a.bin_dir.resolve() not in {Path(p).resolve() for p in os.environ.get("PATH", "").split(os.pathsep) if p}:
        print(f"  note: {a.bin_dir} is not on your PATH; add it for your own shells")

    print("== This host's config")
    if not (_config_home() / "agent-media" / "config.toml").exists():
        _run([*me, "media-setup", "init", "--roles", "origin"], dry_run=a.dry_run)
    env_file = _config_home() / "agent-media.env"
    lines = env_file.read_text().splitlines() if env_file.exists() else []
    if not any(line.startswith("MEDIA_HEADLESS=") for line in lines):
        print(f"  MEDIA_HEADLESS=1 → {env_file}")
        if not a.dry_run:
            env_file.parent.mkdir(parents=True, exist_ok=True)
            with env_file.open("a") as f:
                f.write("MEDIA_HEADLESS=1\n")

    print("== Agents")
    path = os.pathsep.join([str(a.bin_dir), os.environ.get("PATH", "")])
    if shutil.which("claude", path=path):
        _run([*me, "media-setup", "install-hooks"], dry_run=a.dry_run)
    else:
        print("  Claude Code: not installed")
    if shutil.which("opencode", path=path) or (Path.home() / ".opencode" / "bin" / "opencode").exists():
        _run([*me, "media-setup", "profile", "--only", "opencode"], dry_run=a.dry_run)
    else:
        print("  opencode: not installed")

    if a.no_services:
        return 0
    print("== Services")
    if sys.platform == "darwin":
        return _launchd(binary, a)
    # A container, or an ssh login with no user manager, has systemctl but no
    # --user bus to reach.
    if shutil.which("systemctl") is None or subprocess.run(
            ["systemctl", "--user", "show-environment"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        print("  no systemd --user here: start `sasonica serve` and `sasonica sessiond` yourself")
        return 0
    units = _config_home() / "systemd" / "user"
    if (units / CHECKOUT_CANVAS_UNIT).exists() and not a.force:
        print(f"  {CHECKOUT_CANVAS_UNIT} already serves this host (a checkout install); "
              "left alone — --force installs sasonica's beside it")
        return 0
    for name, (word, what) in UNITS.items():
        print(f"  {units / (name + '.service')}")
        if not a.dry_run:
            units.mkdir(parents=True, exist_ok=True)
            (units / f"{name}.service").write_text(unit_text(binary, word, what, a.bind, a.port))
    _run(["systemctl", "--user", "daemon-reload"], dry_run=a.dry_run)
    rc = _run(["systemctl", "--user", "enable", "--now", *(f"{n}.service" for n in UNITS)],
              dry_run=a.dry_run)
    # Restart too: after a new binary, enable --now leaves the old one running.
    _run(["systemctl", "--user", "restart", *(f"{n}.service" for n in UNITS)], dry_run=a.dry_run)
    print("  (they stop at logout unless lingering is on: loginctl enable-linger)")
    return rc
