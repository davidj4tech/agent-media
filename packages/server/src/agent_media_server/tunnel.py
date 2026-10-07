"""The quick tunnel (roadmap item 15, "for other users";
docs/proposals/2026-10-03-off-the-tailnet.md).

A stranger's install has no tailnet and no domain. `sasonica install` puts
cloudflared in ~/.local/bin and a service that runs `media-tunnel run`:
`cloudflared tunnel --url http://<MEDIA_VISUAL_PUBLIC>`, a quick tunnel (no
Cloudflare account) to the app-routes-only listener. It names itself
`https://<random>.trycloudflare.com` on stderr; that URL goes to
`<state_dir>/tunnel.json`, which `pair --device` reads (canvas._cmd_pair), so
the link names the tunnel, not a tailnet address.

The name changes every time the tunnel restarts (a reboot, a crash), and the
phone then has to be paired again. A sasonica.com lookup that would let the
app find the new name is on hold pending Matrix (David, 8 Oct 2026; branch
`lookup-hold`).

Config (env):
  MEDIA_VISUAL_PUBLIC   the listener the tunnel points at (default 127.0.0.1:8789)
  MEDIA_CLOUDFLARED     the cloudflared to run (default ~/.local/bin/cloudflared,
                        else PATH)

    media-tunnel run            the service: cloudflared, its URL kept
    media-tunnel url [--wait S] the current tunnel URL (exit 1 when none)
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from agent_media_core import procinfo
from agent_media_core._paths import state_dir

DEFAULT_PUBLIC = "127.0.0.1:8789"

#: The cloudflared `sasonica install` fetches: Cloudflare's GitHub release,
#: pinned, with each asset's sha256 (the release's own digests, 2026.9.3 — the
#: version red5's named tunnel has run since 3 Oct 2026).
CLOUDFLARED_VERSION = "2026.9.3"
CLOUDFLARED_ASSETS = {
    ("linux", "x86_64"): ("cloudflared-linux-amd64",
                          "77e26d8d900e0b8469f416239d14b5f296525fdf79fee6f511ef55609e3fbac2"),
    ("linux", "aarch64"): ("cloudflared-linux-arm64",
                           "aaeb2d7d0da3614634c7e03ab13487a1522c2e79165ed2929cfe23d5e95b326d"),
    ("darwin", "arm64"): ("cloudflared-darwin-arm64.tgz",
                          "587c2cfb1c230fe36c7fa7727da78be459dae028cabe8c001291999350f07095"),
}
_RELEASES = "https://github.com/cloudflare/cloudflared/releases/download"

_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


# --- the tunnel's URL --------------------------------------------------------------

def state_path() -> Path:
    return state_dir() / "tunnel.json"


def current_url() -> str:
    """The running quick tunnel's URL, or "" (none, or its runner is gone)."""
    try:
        got = json.loads(state_path().read_text())
    except (OSError, ValueError):
        return ""
    url, pid = got.get("url") or "", int(got.get("pid") or 0)
    return url if url and pid and procinfo.alive(pid) else ""


def _write_state(url: str) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"url": url, "pid": os.getpid(), "since": round(time.time())}))
    tmp.replace(p)


def _clear_state() -> None:
    try:
        got = json.loads(state_path().read_text())
        if int(got.get("pid") or 0) == os.getpid():
            state_path().unlink()
    except (OSError, ValueError):
        pass


# --- cloudflared ---------------------------------------------------------------------

def cloudflared_path() -> str:
    want = os.environ.get("MEDIA_CLOUDFLARED")
    if want:
        return want
    local = Path.home() / ".local" / "bin" / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
    if local.exists():
        return str(local)
    return shutil.which("cloudflared") or ""


def platform_asset() -> tuple[str, str] | None:
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        return CLOUDFLARED_ASSETS.get(("darwin", "arm64" if machine in ("arm64", "aarch64") else machine))
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    return CLOUDFLARED_ASSETS.get(("linux", machine)) if sys.platform.startswith("linux") else None


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_cloudflared(bin_dir: Path, *, dry_run: bool = False) -> tuple[str, str]:
    """cloudflared into `bin_dir`, checked against its pinned sha256.
    `(path or "", what happened)`. One already there is used as it is."""
    dest = bin_dir / "cloudflared"
    if dest.exists():
        return str(dest), f"{dest} (already there)"
    found = shutil.which("cloudflared")
    if found:
        return found, f"{found} (on PATH)"
    asset = platform_asset()
    if not asset:
        return "", f"no cloudflared build for {sys.platform} {platform.machine()}"
    name, want = asset
    url = f"{_RELEASES}/{CLOUDFLARED_VERSION}/{name}"
    if dry_run:
        return str(dest), f"would fetch {url}"
    bin_dir.mkdir(parents=True, exist_ok=True)
    tmp = bin_dir / (name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "sasonica-install/1"})
    with urllib.request.urlopen(req, timeout=120) as r, tmp.open("wb") as f:
        shutil.copyfileobj(r, f)
    got = _sha256(tmp)
    if got != want:
        tmp.unlink()
        return "", f"{name}: checksum mismatch ({got[:12]}… ≠ {want[:12]}…); not installed"
    if name.endswith(".tgz"):
        import tarfile

        with tarfile.open(tmp) as tf:
            member = next(m for m in tf.getmembers() if m.isfile() and m.name.endswith("cloudflared"))
            src = tf.extractfile(member)
            assert src is not None
            with (bin_dir / "cloudflared.new").open("wb") as out:
                shutil.copyfileobj(src, out)
        tmp.unlink()
        tmp = bin_dir / "cloudflared.new"
    tmp.chmod(0o755)
    tmp.replace(dest)
    return str(dest), f"{dest} ({CLOUDFLARED_VERSION}, sha256 checked)"


def origin() -> str:
    pub = os.environ.get("MEDIA_VISUAL_PUBLIC") or DEFAULT_PUBLIC
    host, _, port = pub.rpartition(":")
    return f"http://{host or '127.0.0.1'}:{port}"


def run() -> int:
    """The service: cloudflared's quick tunnel, its URL written down.
    Exits with cloudflared (the service manager starts it again)."""
    cf = cloudflared_path()
    if not cf:
        print("media-tunnel: no cloudflared (sasonica install fetches it)", file=sys.stderr)
        return 2
    # An empty config: a ~/.cloudflared/config.yml left by a named tunnel
    # would otherwise be read into the quick tunnel.
    empty = state_dir() / "cloudflared-quick.yml"
    empty.parent.mkdir(parents=True, exist_ok=True)
    if not empty.exists():
        empty.write_text("# media-tunnel: a quick tunnel takes no config\n")
    argv = [cf, "tunnel", "--no-autoupdate", "--config", str(empty), "--url", origin()]
    print("media-tunnel: " + " ".join(argv), file=sys.stderr, flush=True)
    child = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             text=True, errors="replace")
    def _term(*_a) -> None:
        child.terminate()     # its stderr closes, and the loop below ends

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _term)
        signal.signal(signal.SIGINT, _term)
    url = ""
    try:
        assert child.stderr is not None
        for line in child.stderr:
            sys.stderr.write(line)
            m = _URL_RE.search(line)
            if m and m.group(0) != url and "api.trycloudflare.com" not in m.group(0):
                url = m.group(0)
                _write_state(url)
                print(f"media-tunnel: {url}", file=sys.stderr, flush=True)
        return child.wait()
    finally:
        _clear_state()
        if child.poll() is None:
            child.terminate()


def main(argv: list[str] | None = None) -> int:
    import argparse

    from agent_media_core.intake._env import load_env_file

    load_env_file("tunnel")
    ap = argparse.ArgumentParser(prog="media-tunnel", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    u = sub.add_parser("url")
    u.add_argument("--wait", type=float, default=0.0, metavar="S")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        return run()
    if a.cmd == "url":
        deadline = time.monotonic() + a.wait
        while True:
            url = current_url()
            if url or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        if url:
            print(url)
            return 0
        print("no quick tunnel is running", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
