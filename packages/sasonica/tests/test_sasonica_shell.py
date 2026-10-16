"""`sasonica shell …` and Shell's words under the server's command (David,
16 Oct 2026): one `sasonica` where the server and Sasonica Shell are both
installed. Nothing here runs Node: subprocess.call is a stand-in."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sasonica import __main__ as sas
from sasonica import shell

posix = pytest.mark.skipif(os.name == "nt", reason="Linux and macOS paths")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    for k in ("SASONICA_CONF", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA", shell.VIA_ENV):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("SASONICA_CONF", str(h / "conf"))
    monkeypatch.setenv("XDG_DATA_HOME", str(h / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(h / "data"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    return h


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def call(argv, env=None):
        seen.append((list(argv), env))
        return 7
    monkeypatch.setattr(shell.subprocess, "call", call)
    return seen


def runner_at(home: Path, where="shell-src") -> Path:
    r = home / where / "sasonica.mjs"
    r.parent.mkdir(parents=True, exist_ok=True)
    r.write_text("// stand-in\n")
    return r


def node_at(home: Path) -> Path:
    n = home / "node" / "node"
    n.parent.mkdir(parents=True, exist_ok=True)
    n.write_text("")
    return n


def test_parse_shim_both_kinds():
    assert shell.parse_shim('#!/bin/sh\nexec "/n/node" "/s/sasonica.mjs" "$@"\n') == ("/n/node", "/s/sasonica.mjs")
    assert shell.parse_shim('@echo off\r\n"C:\\n\\node.exe" "C:\\a b\\sasonica.mjs" %*\r\n') == \
        ("C:\\n\\node.exe", "C:\\a b\\sasonica.mjs")
    assert shell.parse_shim('#!/bin/sh\n# sasonica shim\nexec "/opt/sasonica" media "$@"\n') is None
    assert shell.parse_shim("\x7fELF\x02\x01") is None


def test_the_record_finds_shell(home, calls):
    runner, node = runner_at(home), node_at(home)
    shell.write_record(str(node), str(runner))
    assert shell.find_runner() == (str(node), str(runner))
    assert sas.main(["shell", "--once", "x"]) == 7
    argv, env = calls[-1]
    assert argv == [str(node), str(runner), "--once", "x"]
    assert env[shell.VIA_ENV] == "1"


def test_a_node_that_went_is_the_one_on_path(home, monkeypatch):
    runner = runner_at(home)
    shell.write_record("/gone/node", str(runner))
    monkeypatch.setattr(shell.shutil, "which", lambda name: "/usr/bin/node")
    assert shell.find_runner() == ("/usr/bin/node", str(runner))


def test_a_record_whose_runner_went_is_not_shell(home, monkeypatch):
    shell.write_record(str(node_at(home)), str(home / "gone" / "sasonica.mjs"))
    assert shell.find_runner() is None


@posix
def test_a_shim_finds_shell_and_then_the_hosted_place(home, monkeypatch):
    monkeypatch.setattr(shell.shutil, "which", lambda name: "/usr/bin/node")
    runner, node = runner_at(home), node_at(home)
    b = home / ".local" / "bin"
    b.mkdir(parents=True)
    (b / "sasonica-shell").write_text(f'#!/bin/sh\nexec "{node}" "{runner}" "$@"\n')
    assert shell.find_runner() == (str(node), str(runner))
    (b / "sasonica-shell").unlink()
    assert shell.find_runner() is None
    hosted = runner_at(home, "data/sasonica/shell")
    assert shell.find_runner() == ("/usr/bin/node", str(hosted))


def test_shell_words_forward(home, calls):
    runner, node = runner_at(home), node_at(home)
    shell.write_record(str(node), str(runner))
    for argv in (["status", "3"], ["skills"], ["url", "--name", "desk"], ["client", "list"],
                 ["tools"], ["sign", "n", "c"], ["--once"]):
        assert sas.main(argv) == 7
        assert calls[-1][0] == [str(node), str(runner), *argv]


def test_shell_words_without_shell_say_how_to_get_it(home, calls, capsys):
    assert sas.main(["status"]) == 1
    err = capsys.readouterr().err
    assert "Sasonica Shell is not installed" in err and "sasonica shell install" in err
    assert shell.get_line() in err
    assert calls == []


def test_shell_install_without_shell_runs_the_one_liner(home, calls):
    assert sas.main(["shell", "install"]) == 7
    argv = calls[-1][0]
    assert shell.get_line() in argv[-1]


def test_server_words_stay_the_servers(home, calls, monkeypatch):
    shell.write_record(str(node_at(home)), str(runner_at(home)))
    seen = {}
    monkeypatch.setattr(sas, "run_script", lambda name, args: seen.setdefault("x", (name, args)) and 0)
    sas.main(["devices", "--revoke", "a"])
    assert seen["x"] == ("media-visual-canvas", ["devices", "--revoke", "a"])
    assert calls == []
    assert sas.main(["no-such-thing"]) == 0     # run_script's, not Shell's


def test_help_lists_shell_when_it_is_here(home, capsys):
    assert sas.main([]) == 0
    out = capsys.readouterr().out
    assert "not installed: `sasonica shell install`" in out
    shell.write_record(str(node_at(home)), str(runner_at(home)))
    assert sas.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "Sasonica Shell, installed" in out and "sasonica status" in out


@posix
def test_install_adopts_a_sasonica_shell_shim(home):
    runner, node = runner_at(home), node_at(home)
    b = home / ".local" / "bin"
    b.mkdir(parents=True)
    shim = b / "sasonica-shell"
    shim.write_text(f'#!/bin/sh\nexec "{node}" "{runner}" "$@"\n')
    (b / "media").write_text("#!/bin/sh\n# sasonica shim\nexec x\n")
    assert shell.adopt_shims(b, dry_run=True) == [shim] and shim.exists()
    assert shell.adopt_shims(b) == [shim]
    assert not shim.exists() and (b / "media").exists()
    assert shell.read_record() == (str(node), str(runner))
    # A record naming a runner that exists is kept.
    other = runner_at(home, "other")
    shell.write_record("/x/node", str(other))
    shim.write_text(f'#!/bin/sh\nexec "{node}" "{runner}" "$@"\n')
    shell.adopt_shims(b)
    assert shell.read_record() == ("/x/node", str(other))
