"""`sasonica update` — the newest binary in place of this one.

  1. fetches `SHA256SUMS` from the server-latest release and, when this
     binary is not the newest, the newest one for this machine, checked
     against it; renamed over this file, so running services keep the file
     they started from;
  2. restarts the server (`sasonica-canvas`), and the session holder too when
     no chat is live in it — a restart ends its chats (they resume on their
     next message, but a turn in progress is cut off). With chats live it is
     left on the old version and says so; `--restart-sessiond` restarts it
     anyway;
  3. removes the unpacked copies of older versions (~180 MB each) that no
     running process still uses.

Where there is no systemd --user (the phone, whose services are Termux's),
it replaces the file and says what to restart. SASONICA_BINARY_BASE is where
the release is (tests: a file:// directory).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from sasonica.__main__ import binary_path

BASE = "https://github.com/davidj4tech/agent-media/releases/download/server-latest"
CANVAS, SESSIOND = "sasonica-canvas.service", "sasonica-sessiond.service"


def base() -> str:
    return (os.environ.get("SASONICA_BINARY_BASE") or BASE).rstrip("/")


def asset() -> str:
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64",
            "arm64": "aarch64"}.get(platform.machine().lower(), "")
    if not arch:
        raise SystemExit(f"sasonica update: no build for {platform.machine()}")
    if os.name == "nt":
        return f"sasonica-windows-{arch}.exe"
    return f"sasonica-{'macos' if sys.platform == 'darwin' else 'linux'}-{arch}"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, dest: Path | None = None, timeout: float = 600):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        if dest is None:
            return r.read()
        with open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    return None


def wanted_sum(sums: str, name: str) -> str:
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0]
    return ""


def _executables() -> list[Path]:
    """Every running process's executable: /proc on Linux, `ps` where there
    is none (a Mac, whose `comm` is the full path)."""
    proc = Path("/proc")
    if proc.is_dir():
        out = []
        for d in proc.iterdir():
            if d.name.isdigit():
                try:
                    out.append(Path(os.readlink(d / "exe")))
                except OSError:
                    pass
        return out
    try:
        ps = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    return [Path(line.strip()) for line in ps.stdout.splitlines() if line.strip().startswith("/")]


def in_use(root: Path) -> set[Path]:
    """The version directories under `root` a running process executes from."""
    used = set()
    for exe in _executables():
        try:
            rel = exe.relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) >= 2:
            used.add(root / rel.parts[0] / rel.parts[1])
    return used


def prune(root: Path, keep: set[Path], *, dry_run: bool = False) -> list[Path]:
    """Remove `<root>/<distribution id>/<version>` directories not in `keep`
    and not in use; the removed ones."""
    gone = []
    busy = in_use(root) | {k.resolve() for k in keep}
    for dist in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        for ver in sorted(p for p in dist.iterdir() if p.is_dir()):
            if ver.resolve() in busy or ver in busy:
                continue
            gone.append(ver)
            if not dry_run:
                shutil.rmtree(ver, ignore_errors=True)
    return gone


def _systemd() -> bool:
    return shutil.which("systemctl") is not None and subprocess.run(
        ["systemctl", "--user", "show-environment"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def _live_chats() -> int:
    """How many chats sessiond holds live; 0 when it cannot be asked."""
    try:
        from agent_media_server.driver.headless import call
    except ImportError:
        return 0
    r = call("list", timeout=5.0)
    return sum(1 for s in r.get("sessions") or [] if s.get("live")) if r.get("ok") else 0


def _restart_tasks(force_sessiond: bool) -> None:
    """Windows's two logon tasks (install.TASKS), ended and run again."""
    from sasonica.install import TASKS

    tasks = [TASKS["sasonica-canvas"]]
    live = _live_chats()
    if live and not force_sessiond:
        print(f"  the session holder has {live} live chat(s): left on the old version until "
              "you run `sasonica update --restart-sessiond` (that ends them)")
    else:
        tasks.append(TASKS["sasonica-sessiond"])
    for task in tasks:
        subprocess.run(["schtasks", "/End", "/TN", task], capture_output=True)
        subprocess.run(["schtasks", "/Run", "/TN", task], capture_output=True)
    print("  restarted " + ", ".join(tasks))


def _restart_launchd(force_sessiond: bool) -> None:
    """A Mac's two launchd agents (install.LABELS), the session holder only
    with no chat live in it, as on Linux."""
    from sasonica.install import LABELS

    domain = f"gui/{os.getuid()}"
    labels = [LABELS["sasonica-canvas"]]
    live = _live_chats()
    if live and not force_sessiond:
        print(f"  the session holder has {live} live chat(s): left on the old version until "
              "you run `sasonica update --restart-sessiond` (that ends them)")
    else:
        labels.append(LABELS["sasonica-sessiond"])
    for label in labels:
        subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{label}"])
    print("  restarted " + ", ".join(labels))


def pyapp_root() -> Path:
    """Where PyApp unpacks this binary's versions: its platform's data
    directory (the `directories` crate's)."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "pyapp" / "sasonica"
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data / "pyapp" / "sasonica"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sasonica update", description=__doc__.split("\n\n")[0])
    ap.add_argument("--binary", help="the binary to replace (default: this one)")
    ap.add_argument("--restart-sessiond", action="store_true",
                    help="restart the session holder even with chats live (ends them)")
    ap.add_argument("--check", action="store_true", help="only say whether there is a newer one")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)

    binary = a.binary or binary_path()
    if not binary:
        print("sasonica update: not running from the binary (a checkout updates with git pull)",
              file=sys.stderr)
        return 2
    binary_p = Path(binary).resolve()
    name = asset()
    try:
        want = wanted_sum(fetch(f"{base()}/SHA256SUMS", timeout=30).decode(), name)
    except OSError as e:
        print(f"sasonica update: could not reach the release ({e})", file=sys.stderr)
        return 1
    if not want:
        print(f"sasonica update: the release has no {name}", file=sys.stderr)
        return 1
    if sha256(binary_p) == want:
        print("sasonica is up to date")
        return 0
    if a.check:
        print("a newer sasonica is available: sasonica update")
        return 0

    new = binary_p.with_name(binary_p.name + ".new")
    print(f"== Downloading {name}")
    try:
        fetch(f"{base()}/{name}", new)
    except OSError as e:
        new.unlink(missing_ok=True)
        print(f"sasonica update: download failed ({e})", file=sys.stderr)
        return 1
    if sha256(new) != want:
        new.unlink(missing_ok=True)
        print("sasonica update: the download does not match its checksum; nothing changed",
              file=sys.stderr)
        return 1
    new.chmod(0o755)
    if os.name == "nt":
        # A running .exe cannot be replaced, but it can be renamed: the old
        # one steps aside (removed next time, when nothing runs it).
        old = binary_p.with_name(binary_p.name + ".old")
        try:
            old.unlink(missing_ok=True)
        except OSError:
            pass
        os.replace(binary_p, old)
    os.replace(new, binary_p)
    # Unpacked now, so the restart below starts at once.
    try:
        out = subprocess.run([str(binary_p), "version"], capture_output=True, text=True).stdout
    except OSError as e:             # not runnable here: say so, it is installed
        out = f"installed (it did not start: {e})"
    print(f"  now {out.strip() or 'installed'}")

    print("== Services")
    if os.name == "nt":
        _restart_tasks(a.restart_sessiond)
        # Unpacked versions stay: Windows gives no simple way to see which a
        # running process uses. They are under %LOCALAPPDATA%\pyapp\data.
        return 0
    if sys.platform == "darwin":
        _restart_launchd(a.restart_sessiond)
    elif not _systemd():
        print("  no systemd --user here: restart the server and the session holder yourself "
              "(on the phone: sv restart sasonica-canvas sasonica-sessiond, in Termux)")
        return 0
    else:
        units = [CANVAS]
        live = _live_chats()
        if live and not a.restart_sessiond:
            print(f"  the session holder has {live} live chat(s): left on the old version until "
                  "you run `sasonica update --restart-sessiond` (that ends them)")
        else:
            units.append(SESSIOND)
        subprocess.run(["systemctl", "--user", "restart", *units])
        print("  restarted " + ", ".join(u.removesuffix(".service") for u in units))

    print("== Old versions")
    # The newest unpacked is the one just installed, whatever runs it yet.
    root = pyapp_root()
    vers = [v for d in root.iterdir() if d.is_dir() for v in d.iterdir() if v.is_dir()] \
        if root.is_dir() else []
    gone = prune(root, {max(vers, key=lambda v: v.stat().st_mtime)} if vers else set())
    print(f"  removed {len(gone)}" if gone else "  none to remove")
    return 0
