"""Sasonica Shell under the server's `sasonica` (David, 16 Oct 2026): one
command where both are installed.

Shell (github.com/davidj4tech/sasonica-shell) is a Node program, its runner
`sasonica.mjs`. Alone on a computer its installer puts a `sasonica` command on
PATH (a two-line shim: `exec "<node>" "<runner>" "$@"`, or a .cmd on Windows).
Beside the server, whose binary is `sasonica`, it writes no command at all and
the server runs it:

    sasonica shell [args…]    Shell's own command, whatever the arguments
    sasonica status | skills | tools | url | client … | sign … | --once
                              Shell's words the server has none of, forwarded

Where its runner is: the record Shell's installer writes in its config folder,
`runner` (two lines, the node to run it with and the runner's path), the same
folder Shell reads (SASONICA_CONF, else %APPDATA%\\sasonica on Windows, else
~/.config/sasonica). Before that record existed, a shim's exec line said it, so
a Shell shim found on the way is read too, and adopted: `sasonica install`
records its runner and removes it (`adopt_shims`), the `sasonica-shell` name the
first side-by-side installs gave it among them. Last, the hosted install's own
place, ~/.local/share/sasonica/shell (%LOCALAPPDATA%\\sasonica\\shell).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

#: Shell's words the server has none of: `sasonica <word>` runs Shell.
#: (`install`, `pair`, `help` and no words are the server's; `devices` is the
#: canvas's, as Shell's own `devices` is.)
WORDS = ("sign", "skills", "tools", "client", "url", "status", "--once")

#: What a person without Shell is told to run.
GET_POSIX = "curl -fsSL https://sasonica.com/install | bash -s shell"
GET_WINDOWS = "irm https://sasonica.com/shell.ps1 | iex"

#: Told to Shell, so its help and messages name `sasonica shell …`.
VIA_ENV = "SASONICA_SHELL_VIA_SERVER"

_EXEC = re.compile(r'"([^"\r\n]+)"\s+"([^"\r\n]*sasonica\.mjs)"')


def conf_dir() -> Path:
    """Shell's config folder, as its runner and installer find it."""
    if os.environ.get("SASONICA_CONF"):
        return Path(os.environ["SASONICA_CONF"])
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "sasonica"
    return Path.home() / ".config" / "sasonica"


def record_path() -> Path:
    return conf_dir() / "runner"


def shim_dirs(bin_dir: Path | None = None) -> list[Path]:
    """Where Shell's installers put its command."""
    if os.name == "nt":
        return [conf_dir() / "bin"]
    return [bin_dir or Path.home() / ".local" / "bin"]


def shim_names() -> list[str]:
    return ["sasonica.cmd", "sasonica-shell.cmd"] if os.name == "nt" else ["sasonica-shell", "sasonica"]


def parse_shim(text: str) -> tuple[str, str] | None:
    """(node, runner) from a Shell shim's exec line, or None if it is not one."""
    m = _EXEC.search(text or "")
    return (m.group(1), m.group(2)) if m else None


def _read(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_bytes()[:400].decode("latin-1")
    except OSError:
        return None


def read_record() -> tuple[str, str] | None:
    try:
        lines = [x.strip() for x in record_path().read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        return None
    return (lines[0], lines[1]) if len(lines) >= 2 else None


def write_record(node: str, runner: str) -> None:
    p = record_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{node}\n{runner}\n", encoding="utf-8")


def _usable(node: str, runner: str) -> tuple[str, str] | None:
    """The pair if its runner is there; a node that has gone (an upgraded
    fnm or nvm version) is replaced by the one on PATH."""
    if not runner or not Path(runner).is_file():
        return None
    if not node or not Path(node).exists():
        node = shutil.which("node") or ""
    return (node, runner) if node else None


def find_runner(bin_dir: Path | None = None) -> tuple[str, str] | None:
    """(node, runner) for Sasonica Shell on this computer, or None."""
    rec = read_record()
    if rec and (got := _usable(*rec)):
        return got
    for d in shim_dirs(bin_dir):
        for name in shim_names():
            parsed = parse_shim(_read(d / name) or "")
            if parsed and (got := _usable(*parsed)):
                return got
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return _usable("", str(base / "sasonica" / "shell" / "sasonica.mjs"))


def adopt_shims(bin_dir: Path | None = None, *, dry_run: bool = False) -> list[Path]:
    """Shell's commands that would be a second name beside the server's
    `sasonica`: their runner recorded, the files removed. Returns them.

    On Linux and a Mac that is ~/.local/bin/sasonica-shell (the server's
    binary is ~/.local/bin/sasonica, so a Shell shim of that name is never
    there by the time this runs); on Windows both of Shell's .cmd files in
    %APPDATA%\\sasonica\\bin, which only shadowed or were shadowed by the
    server's sasonica.exe."""
    gone: list[Path] = []
    for d in shim_dirs(bin_dir):
        names = shim_names() if os.name == "nt" else ["sasonica-shell"]
        for name in names:
            p = d / name
            parsed = parse_shim(_read(p) or "")
            if not parsed:
                continue
            gone.append(p)
            if dry_run:
                continue
            rec = read_record()
            if not (rec and _usable(*rec)):
                write_record(*parsed)
            p.unlink()
    return gone


def get_line() -> str:
    return GET_WINDOWS if os.name == "nt" else GET_POSIX


def not_installed(what: str) -> int:
    print(f"sasonica {what}: Sasonica Shell is not installed on this computer. It lets an AI\n"
          f"assistant such as Claude run commands here; to install it:\n\n"
          f"    sasonica shell install\n\n"
          f"(the same as: {get_line()})", file=sys.stderr)
    return 1


def install_hosted() -> int:
    """`sasonica shell install` with no Shell yet: its hosted install, the
    one-liner, run here."""
    if os.name == "nt":
        argv = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", GET_WINDOWS]
    else:
        argv = ["bash", "-c", "set -o pipefail; " + GET_POSIX]
    print(f"Installing Sasonica Shell: {get_line()}")
    try:
        return subprocess.call(argv)
    except OSError as e:
        print(f"sasonica shell install: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def run(args: list[str], *, what: str = "shell") -> int:
    """Shell's runner with these arguments; its exit status."""
    found = find_runner()
    if found is None:
        if args[:1] == ["install"] and what == "shell":
            return install_hosted()
        return not_installed(what)
    node, runner = found
    env = dict(os.environ, **{VIA_ENV: "1"})
    try:
        return subprocess.call([node, runner, *args], env=env)
    except OSError as e:
        print(f"sasonica {what}: could not run {node}: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
