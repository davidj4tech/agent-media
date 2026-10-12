"""procinfo.kill_group: os.killpg where there are process groups, a process
tree through taskkill on Windows — `media replay` stops a prior follower
with it (cli._stop_replay_trackers)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from agent_media_core import procinfo


@pytest.mark.skipif(sys.platform == "win32", reason="process groups")
def test_posix_is_exactly_killpg(monkeypatch):
    seen = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: seen.append((pid, sig)))
    procinfo.kill_group(123)
    procinfo.kill_group(124, signal.SIGKILL, expect_image="python3")
    assert seen == [(123, signal.SIGTERM), (124, signal.SIGKILL)]


@pytest.mark.skipif(sys.platform == "win32", reason="process groups")
def test_posix_errors_reach_the_caller(monkeypatch):
    def gone(pid, sig):
        raise ProcessLookupError(pid)
    monkeypatch.setattr(os, "killpg", gone)
    with pytest.raises(ProcessLookupError):
        procinfo.kill_group(1)


class _Win:
    """procinfo's Windows branch, with the process table faked."""

    def __init__(self, monkeypatch, *, alive=True, image="python.exe", rc=0):
        self.ran = []
        monkeypatch.setattr(procinfo.os, "name", "nt")
        monkeypatch.setattr(procinfo, "alive", lambda pid: alive)
        monkeypatch.setattr(procinfo, "image", lambda pid: image)

        def run(argv, **kw):
            self.ran.append(argv)
            return SimpleNamespace(returncode=rc, stderr="denied")
        monkeypatch.setattr(procinfo.subprocess, "run", run)


def test_windows_ends_the_tree(monkeypatch):
    w = _Win(monkeypatch)
    procinfo.kill_group(42, signal.SIGTERM, expect_image="Python.exe")
    assert w.ran == [["taskkill", "/PID", "42", "/T", "/F"]]


def test_windows_leaves_a_reused_pid_alone(monkeypatch):
    w = _Win(monkeypatch, image="chrome.exe")
    with pytest.raises(ProcessLookupError):
        procinfo.kill_group(42, expect_image="python.exe")
    assert w.ran == []


def test_windows_a_gone_process_is_a_lookup_error(monkeypatch):
    w = _Win(monkeypatch, alive=False)
    with pytest.raises(ProcessLookupError):
        procinfo.kill_group(42)
    assert w.ran == []


def test_windows_a_refused_kill_is_a_permission_error(monkeypatch):
    _Win(monkeypatch, rc=1)
    with pytest.raises(PermissionError):
        procinfo.kill_group(42)


def test_a_real_child_and_its_child_are_ended():
    """For real on whichever platform runs this: a detached Python that has
    started a grandchild; both are gone afterwards."""
    code = ("import subprocess, sys, time; "
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print(c.pid, flush=True); time.sleep(60)")
    kw = {"start_new_session": True} if os.name != "nt" else {}
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                         text=True, **kw)
    try:
        grandchild = int(p.stdout.readline())
        procinfo.kill_group(p.pid, expect_image=os.path.basename(sys.executable))
        p.wait(15)
        for _ in range(100):
            if not procinfo.alive(grandchild):
                break
            time.sleep(0.1)
        assert not procinfo.alive(grandchild)
    finally:
        if p.poll() is None:
            p.kill()
