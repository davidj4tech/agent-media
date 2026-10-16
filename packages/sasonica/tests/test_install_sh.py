"""deploy/install.sh (sasonica.com/install) and deploy/shell.sh
(sasonica.com/shell) end to end, on stand-ins: a fake server binary in a
file:// release, a fake Sasonica Shell tarball, and a curl that answers the
server's /healthz itself. Nothing reaches GitHub, the hosted relay or this
machine's real server; HOME is a temporary directory.

The Shell step (David, 16 Oct 2026): one question after the server, default
No; no terminal is No; SASONICA_WITH_SHELL=1/0 answers it; a Shell that fails
does not fail the install; SASONICA_ONLY=shell (or `bash -s shell`, or
sasonica.com/shell) is Shell alone, no server."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
INSTALL = DEPLOY / "install.sh"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="install.sh is Linux and macOS")

FAKE_SERVER = """#!/bin/sh
# fake sasonica server
echo "$*" >>"$HOME/server-calls"
case "$1" in
  version) echo "sasonica 0.0.0-test" ;;
  pair) echo "PAIR-LINK-FOR-TEST" ;;
esac
exit 0
"""

# What Shell's install.mjs does to ~/.local/bin/sasonica (rm, then its own
# two-line shim), and its exit status from SHELL_STUB_EXIT.
FAKE_SHELL_INSTALL = """#!/usr/bin/env bash
echo "$*" >>"$HOME/shell-calls"
echo "    Open this link: https://relay.example.invalid/join?x=1   code: TEST-CODE"
mkdir -p "$HOME/.local/bin"
rm -f "$HOME/.local/bin/sasonica"
printf '#!/bin/sh\\nexec "node" "%s/sasonica.mjs" "$@"\\n' "$(dirname "$0")" >"$HOME/.local/bin/sasonica"
chmod +x "$HOME/.local/bin/sasonica"
exit "${SHELL_STUB_EXIT:-0}"
"""

SHELL_SHIM = '#!/bin/sh\nexec "node" "/old/sasonica-shell/sasonica.mjs" "$@"\n'


def _asset() -> str | None:
    system, machine = platform.system(), platform.machine().lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(machine)
    if system == "Linux" and arch:
        return f"sasonica-linux-{arch}"
    if system == "Darwin" and arch == "aarch64":
        return "sasonica-macos-aarch64"
    return None


@pytest.fixture
def world(tmp_path):
    asset = _asset()
    if asset is None:
        pytest.skip("no server build for this platform")
    real_curl = shutil.which("curl")
    if not real_curl:
        pytest.skip("no curl")
    home = tmp_path / "home"
    home.mkdir()
    rel = tmp_path / "rel"
    rel.mkdir()
    (rel / asset).write_text(FAKE_SERVER)
    digest = hashlib.sha256(FAKE_SERVER.encode()).hexdigest()
    (rel / "SHA256SUMS").write_text(f"{digest}  {asset}\n")
    src = tmp_path / "shellsrc" / "sasonica-shell-main"
    src.mkdir(parents=True)
    (src / "install.sh").write_text(FAKE_SHELL_INSTALL)
    (src / "sasonica.mjs").write_text("// stand-in\n")
    tarball = tmp_path / "shell.tar.gz"
    with tarfile.open(tarball, "w:gz") as t:
        t.add(src, arcname="sasonica-shell-main")
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "curl").write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do case "$a" in *127.0.0.1:8781*) exit 0 ;; esac; done\n'
        f'exec "{real_curl}" "$@"\n')
    (fakebin / "curl").chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SASONICA_", "MEDIA_", "XDG_"))}
    env.update(HOME=str(home), PATH=f"{fakebin}:{env.get('PATH', '/usr/bin:/bin')}",
               SASONICA_BINARY_BASE=rel.as_uri(), SASONICA_SHELL_TARBALL=tarball.as_uri(),
               SASONICA_INSTALL_BASE=DEPLOY.as_uri())
    env.pop("PREFIX", None)
    return {"home": home, "env": env, "server": FAKE_SERVER}


def run(world, *args, script=INSTALL, **extra):
    env = dict(world["env"], **extra)
    # A new session: no controlling terminal, so /dev/tty cannot be opened,
    # as under CI or `curl | bash` from a service.
    return subprocess.run(["bash", str(script), *args], env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=120, start_new_session=True)


def calls(world, name):
    f = world["home"] / name
    return f.read_text().splitlines() if f.exists() else []


def binary(world):
    return world["home"] / ".local" / "bin" / "sasonica"


def test_no_terminal_is_no(world):
    r = run(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "install" in calls(world, "server-calls")
    assert "PAIR-LINK-FOR-TEST" in r.stdout
    assert calls(world, "shell-calls") == []
    assert "Sasonica Shell" not in r.stdout


def test_with_shell_1_installs_it_and_keeps_the_server(world):
    r = run(world, SASONICA_WITH_SHELL="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert calls(world, "shell-calls") == ["--hosted"]
    assert "TEST-CODE" in r.stdout                      # Shell's sign-in, shown
    # Shell wrote over ~/.local/bin/sasonica; the server's binary is back and
    # Shell's command is sasonica-shell.
    assert binary(world).read_text() == world["server"]
    shim = binary(world).with_name("sasonica-shell")
    assert "sasonica.mjs" in shim.read_text()
    assert "command here is sasonica-shell" in r.stdout
    assert not list(binary(world).parent.glob(".sasonica-server.*"))
    unpacked = world["home"] / ".local" / "share" / "sasonica" / "shell"
    assert (unpacked / "sasonica.mjs").exists()


def test_with_shell_0_skips_it(world):
    r = run(world, SASONICA_WITH_SHELL="0")
    assert r.returncode == 0
    assert calls(world, "shell-calls") == []


def test_a_failing_shell_does_not_fail_the_install(world):
    r = run(world, SASONICA_WITH_SHELL="1", SHELL_STUB_EXIT="3")
    assert r.returncode == 0, r.stdout + r.stderr
    assert calls(world, "shell-calls") == ["--hosted"]
    assert "the server is installed and is not affected" in r.stdout
    assert "curl -fsSL https://sasonica.com/install | bash -s shell" in r.stdout
    assert binary(world).read_text() == world["server"]


def test_an_unreachable_shell_does_not_fail_the_install(world):
    r = run(world, SASONICA_WITH_SHELL="1",
            SASONICA_SHELL_TARBALL=(world["home"] / "missing.tar.gz").as_uri())
    assert r.returncode == 0
    assert "Could not download Sasonica Shell" in r.stderr
    assert "bash -s shell" in r.stdout


def test_shell_only_skips_the_server(world):
    r = run(world, SASONICA_ONLY="shell")
    assert r.returncode == 0, r.stdout + r.stderr
    assert calls(world, "server-calls") == []
    assert calls(world, "shell-calls") == ["--hosted"]
    # Alone, Shell keeps the name `sasonica`, as its own installer leaves it.
    assert "sasonica.mjs" in binary(world).read_text()
    assert not binary(world).with_name("sasonica-shell").exists()
    assert "Pairing" not in r.stdout


def test_shell_only_by_argument_and_its_failure(world):
    r = run(world, "shell", SHELL_STUB_EXIT="1")
    assert r.returncode == 1
    assert calls(world, "server-calls") == []
    assert "Sasonica Shell did not install." in r.stdout
    assert "not affected" not in r.stdout


def test_sasonica_com_shell_is_shell_only(world):
    r = run(world, script=DEPLOY / "shell.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert calls(world, "server-calls") == []
    assert calls(world, "shell-calls") == ["--hosted"]


def test_only_takes_shell_alone(world):
    r = run(world, SASONICA_ONLY="server")
    assert r.returncode == 2 and "SASONICA_ONLY" in r.stderr


def test_a_shell_installed_first_moves_aside_for_the_server(world):
    b = binary(world)
    b.parent.mkdir(parents=True)
    b.write_text(SHELL_SHIM)
    r = run(world, SASONICA_WITH_SHELL="0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert b.read_text() == world["server"]
    assert b.with_name("sasonica-shell").read_text() == SHELL_SHIM
    assert "now sasonica-shell" in r.stdout


def _on_a_terminal(world, answer: bytes, **extra):
    """install.sh with a terminal of its own (a pty as its controlling tty)
    and stdin a pipe, as under `curl | bash`: the question goes to the tty."""
    import fcntl      # not on Windows, where this module is skipped
    import termios

    master, slave = os.openpty()

    def take_tty():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    os.write(master, answer)
    try:
        r = subprocess.run(["bash", str(INSTALL)], env=dict(world["env"], **extra),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=120, preexec_fn=take_tty, pass_fds=(slave,))
    finally:
        os.close(slave)
        try:
            tty = os.read(master, 65536).decode(errors="replace")
        except OSError:
            tty = ""
        os.close(master)
    return r, tty


@pytest.mark.skipif(platform.system() != "Linux", reason="TIOCSCTTY from preexec_fn: Linux")
def test_the_question_on_a_terminal(world):
    r, tty = _on_a_terminal(world, b"y\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[y/N]" in tty
    assert calls(world, "shell-calls") == ["--hosted"]


@pytest.mark.skipif(platform.system() != "Linux", reason="TIOCSCTTY from preexec_fn: Linux")
def test_enter_on_the_question_is_no(world):
    r, tty = _on_a_terminal(world, b"\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[y/N]" in tty
    assert calls(world, "shell-calls") == []
