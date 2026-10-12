"""The book channel's player on Windows: found and started on the named pipe
that stands in for `sink-book.sock` (sinks/book.py), never an orphan beside
one already there. Faked on Linux; a real mpv on the Windows runner."""

from __future__ import annotations

import os
import shutil
import sys

import pytest

from agent_media_core.sinks import _mpv_ipc as ipc
from agent_media_core.sinks import book


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("MEDIA_BOOK_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MEDIA_BOOK_WEBUI", "")
    monkeypatch.setenv("MEDIA_BOOK_MPRIS", "")


class _Proc:
    def __init__(self, argv):
        self.argv, self.killed = argv, False

    def poll(self):
        return None

    def kill(self):
        self.killed = True


class _Clock:
    """time.time that a sleep moves on, so the 5 s wait passes at once."""

    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _pipes(monkeypatch, *, there=False, answers=False):
    spawned = []
    monkeypatch.setattr(ipc, "_use_pipes", lambda: True)
    monkeypatch.setattr(ipc, "endpoint_exists", lambda ep: there)

    def get_property(sock, name, timeout=2.0):
        if not answers:
            raise ipc.MpvIpcError("no answer")
        return True

    monkeypatch.setattr(ipc, "get_property", get_property)
    monkeypatch.setattr(book.subprocess, "Popen",
                        lambda argv, **kw: spawned.append(_Proc(argv)) or spawned[-1])
    monkeypatch.setattr(book, "time", _Clock())
    return spawned


def test_running_looks_for_the_pipe_not_a_file(monkeypatch):
    asked = []
    monkeypatch.setattr(ipc, "_use_pipes", lambda: True)
    monkeypatch.setattr(ipc, "endpoint_exists", lambda ep: asked.append(ep) or True)
    monkeypatch.setattr(ipc, "get_property", lambda *a, **k: True)
    assert book.SinkBook()._running() is True
    assert asked and str(asked[0]).endswith("sink-book.sock")   # mapped inside


def test_the_player_is_started_on_the_pipe(monkeypatch):
    spawned = _pipes(monkeypatch)
    with pytest.raises(ipc.MpvIpcError):
        book.SinkBook()._ensure_broker()
    (p,) = spawned
    arg = next(a for a in p.argv if a.startswith("--input-ipc-server="))
    assert arg.split("=", 1)[1].startswith("\\\\.\\pipe\\agent-media-")
    assert "sink-book" in arg


def test_a_player_that_never_comes_up_is_not_left_behind(monkeypatch):
    spawned = _pipes(monkeypatch)
    with pytest.raises(ipc.MpvIpcError, match="did not come up"):
        book.SinkBook()._ensure_broker()
    assert spawned[0].killed


def test_a_slow_player_on_the_pipe_is_not_doubled(monkeypatch):
    spawned = _pipes(monkeypatch, there=True, answers=False)
    with pytest.raises(ipc.MpvIpcError, match="did not answer"):
        book.SinkBook()._ensure_broker()
    assert spawned == []


@pytest.mark.skipif(sys.platform == "win32", reason="the socket-file side")
def test_linux_is_unchanged(monkeypatch, tmp_path):
    spawned = []
    monkeypatch.setattr(book.subprocess, "Popen",
                        lambda argv, **kw: spawned.append(_Proc(argv)) or spawned[-1])
    monkeypatch.setattr(book, "time", _Clock())
    b = book.SinkBook()
    b._sock.parent.mkdir(parents=True)
    b._sock.write_text("")                       # a dead broker's socket
    with pytest.raises(ipc.MpvIpcError):
        b._ensure_broker()
    assert not b._sock.exists()                  # cleared, as before
    assert f"--input-ipc-server={b._sock}" in spawned[0].argv
    assert not spawned[0].killed                 # and left to come up, as before


@pytest.mark.skipif(sys.platform != "win32", reason="a real pipe and mpv")
@pytest.mark.skipif(not shutil.which("mpv"), reason="mpv is not installed")
def test_a_real_book_player_comes_up_on_its_pipe(monkeypatch):
    monkeypatch.setenv("MEDIA_BOOK_AO", "null")
    b = book.SinkBook()
    try:
        b._ensure_broker()
        assert b._running()
        b._ensure_broker()                       # found, not started again
        assert ipc.get_property(b._sock, "idle-active") is True
    finally:
        try:
            ipc.command(b._sock, "quit")
        except (OSError, ipc.MpvIpcError):
            pass
