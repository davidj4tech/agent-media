"""The console script in front of the shell helper."""
import pytest
from agent_media_core.entrypoints import audiobook_fetch


class _Execed(Exception):
    """`execv` replaces the process; a double that merely returns would let
    main() run on into the fallback and report the wrong interpreter."""

    def __init__(self, path, argv):
        self.path, self.argv = path, argv


def _catch_exec(monkeypatch):
    def _fake(path, argv):
        raise _Execed(path, argv)
    monkeypatch.setattr(audiobook_fetch.os, "execv", _fake)


def test_it_execs_the_script_with_its_arguments(monkeypatch):
    _catch_exec(monkeypatch)
    with pytest.raises(_Execed) as e:
        audiobook_fetch.main(["--play", "https://youtu.be/abc"])
    seen = {"path": e.value.path, "argv": e.value.argv}
    # The script itself, so its shebang chooses the interpreter.
    assert seen["path"].endswith("bin/audiobook-fetch")
    assert seen["argv"] == [seen["path"], "--play", "https://youtu.be/abc"]


def test_an_install_that_lost_the_exec_bit_still_runs_it_under_bash(monkeypatch,
                                                                    tmp_path):
    """/bin/sh is dash here, and the helper uses arrays: `sh` would fail at the
    first one with a syntax error that reads like a corrupt install."""
    script = tmp_path / "audiobook-fetch"
    script.write_text("#!/usr/bin/env bash\n")
    script.chmod(0o644)
    monkeypatch.setattr("agent_media_core.setup.shipped_bin", lambda name: script)
    _catch_exec(monkeypatch)
    with pytest.raises(_Execed) as e:
        audiobook_fetch.main([])
    assert e.value.path == "/bin/bash"
    assert e.value.argv == ["/bin/bash", str(script)]


def test_a_missing_helper_says_so_rather_than_execing_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr("agent_media_core.setup.shipped_bin",
                        lambda name: tmp_path / "gone")
    monkeypatch.setattr(audiobook_fetch.os, "execv",
                        lambda *a: pytest.fail("must not exec"))
    assert audiobook_fetch.main([]) == 127
