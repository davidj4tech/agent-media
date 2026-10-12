"""mpv IPC over a Windows named pipe (sinks/_mpv_ipc.py).

On Linux the pipe is faked (a file object and a peek function); on Windows a
real pipe server answers, and a real mpv when one is installed.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_media_core.sinks import _mpv_ipc as ipc

win_only = pytest.mark.skipif(sys.platform != "win32", reason="named pipes are Windows'")


# ---- names ---------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="the posix side of the mapping")
def test_paths_are_unchanged_off_windows(tmp_path):
    p = tmp_path / "sink-speech.sock"
    assert ipc.endpoint_name(p) == str(p)
    assert ipc.endpoint_name("tcp://h:1") == "tcp://h:1"


def test_a_path_maps_to_one_pipe_per_user_and_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(ipc, "_use_pipes", lambda: True)
    monkeypatch.setattr(ipc, "_pipe_user", lambda: "Da vid")
    a = ipc.endpoint_name(tmp_path / "sink-speech.sock")
    assert a.startswith("\\\\.\\pipe\\agent-media-Da_vid-sink-speech-")
    assert a == ipc.endpoint_name(str(tmp_path / "sink-speech.sock"))
    assert a != ipc.endpoint_name(tmp_path / "other" / "sink-speech.sock")
    assert ipc.endpoint_name("tcp://h:1") == "tcp://h:1"
    assert ipc.endpoint_name("\\\\.\\pipe\\mine") == "\\\\.\\pipe\\mine"
    assert ipc.endpoint_name("//./pipe/mine") == "//./pipe/mine"


def test_the_pipe_user_is_windows_own_name(monkeypatch):
    """Git Bash may set USER/LOGNAME to something else; the logon task and
    the picker must still name the same pipe."""
    monkeypatch.setenv("USERNAME", "ann")
    monkeypatch.setenv("USER", "msys-ann")
    monkeypatch.setenv("LOGNAME", "msys-ann")
    monkeypatch.setattr(ipc.os, "name", "nt")
    assert ipc._pipe_user() == "ann"
    monkeypatch.setattr(ipc.os, "name", "posix")
    assert ipc._pipe_user() == "msys-ann"


def test_endpoint_exists(monkeypatch, tmp_path):
    p = tmp_path / "s.sock"
    assert ipc.endpoint_exists("tcp://h:1")
    if sys.platform != "win32":
        assert not ipc.endpoint_exists(p)
        p.write_text("")
        assert ipc.endpoint_exists(p)
    asked = []
    monkeypatch.setattr(ipc, "_pipe_exists", lambda n: asked.append(n) or False)
    assert not ipc.endpoint_exists("\\\\.\\pipe\\x")
    assert asked == ["\\\\.\\pipe\\x"]


# ---- the connection, faked ----------------------------------------------------------

class _FakePipe:
    """A pipe's two directions: `inbox` is what the far end has written."""

    def __init__(self, reply=None, chunk=None):
        self.inbox = bytearray()
        self.written = bytearray()
        self.closed = False
        self.eof = False
        self.reply = reply
        self.chunk = chunk

    def write(self, b):
        n = len(b) if self.chunk is None else min(self.chunk, len(b))
        self.written += bytes(b[:n])
        if self.reply:
            self.reply(self)
        return n

    def read(self, n):
        out = bytes(self.inbox[:n])
        del self.inbox[:n]
        return out

    def close(self):
        self.closed = True


def _peek(f):
    if f.eof and not f.inbox:
        return None
    return len(f.inbox)


def test_recv_reads_what_is_waiting():
    f = _FakePipe()
    f.inbox += b"hello\n"
    c = ipc._PipeConn(f, _peek)
    c.settimeout(1)
    assert c.recv(3) == b"hel"
    assert c.recv(4096) == b"lo\n"


def test_recv_times_out_like_a_socket():
    c = ipc._PipeConn(_FakePipe(), _peek)
    c.settimeout(0.05)
    t = time.monotonic()
    with pytest.raises(socket.timeout):
        c.recv(10)
    assert time.monotonic() - t < 1


def test_recv_at_the_far_ends_close_is_eof():
    f = _FakePipe()
    f.eof = True
    assert ipc._PipeConn(f, _peek).recv(10) == b""


def test_sendall_finishes_a_short_write():
    f = _FakePipe(chunk=3)
    ipc._PipeConn(f, _peek).sendall(b"0123456789")
    assert bytes(f.written) == b"0123456789"


def _busy():
    e = OSError("busy")
    e.winerror = ipc._ERROR_PIPE_BUSY
    return e


def test_connect_waits_out_a_busy_pipe():
    tries = []

    def opener(name):
        tries.append(name)
        if len(tries) < 3:
            raise _busy()
        return _FakePipe()

    c = ipc._PipeConn.connect("\\\\.\\pipe\\x", 2.0, opener=opener, peek=_peek)
    assert isinstance(c, ipc._PipeConn) and len(tries) == 3


def test_connect_gives_up_on_a_busy_pipe_at_the_timeout():
    def opener(name):
        raise _busy()
    with pytest.raises(OSError):
        ipc._PipeConn.connect("\\\\.\\pipe\\x", 0.05, opener=opener, peek=_peek)


def test_connect_to_no_pipe_is_file_not_found():
    def opener(name):
        raise FileNotFoundError(name)
    with pytest.raises(FileNotFoundError):
        ipc._PipeConn.connect("\\\\.\\pipe\\x", 1.0, opener=opener, peek=_peek)


def _mpv_like(f):
    """Answer every complete request line the way mpv does."""
    while b"\n" in f.written:
        line, _, rest = bytes(f.written).partition(b"\n")
        f.written = bytearray(rest)
        req = json.loads(line)
        cmd = req["command"]
        out = {"error": "success", "request_id": req.get("request_id")}
        if cmd[0] == "get_property":
            out["data"] = {"idle-active": True, "volume": 130.0}.get(cmd[1])
        f.inbox += (json.dumps({"event": "idle"}) + "\n").encode()
        f.inbox += (json.dumps(out) + "\n").encode()
        f.log.append(cmd)


def test_the_module_speaks_mpv_over_a_pipe(monkeypatch, tmp_path):
    monkeypatch.setattr(ipc, "_use_pipes", lambda: True)
    opened = []

    def fake_connect(name, timeout, opener=None, peek=None):
        f = _FakePipe(reply=_mpv_like)
        f.log = []
        opened.append((name, f))
        return ipc._PipeConn(f, _peek)

    monkeypatch.setattr(ipc._PipeConn, "connect", staticmethod(fake_connect))
    sock = tmp_path / "sink-speech.sock"
    assert ipc.get_property(sock, "volume") == 130.0
    ipc.command_batch(sock, [["set_property", "pause", False], ["stop"]])
    assert ipc.get_properties(sock, ["idle-active", "volume"]) == {
        "idle-active": True, "volume": 130.0}
    name = ipc.endpoint_name(sock)
    assert all(n == name for n, _ in opened)
    assert all(f.closed for _, f in opened)
    assert opened[1][1].log == [["set_property", "pause", False], ["stop"]]


# ---- real pipes, on Windows ------------------------------------------------------------

@win_only
def test_a_real_pipe_server_answers(tmp_path):
    from multiprocessing.connection import Listener

    name = ipc.endpoint_name(tmp_path / "pipe-test.sock")
    assert name.startswith("\\\\.\\pipe\\")
    listener = Listener(address=name, family="AF_PIPE")
    assert ipc.endpoint_exists(tmp_path / "pipe-test.sock")

    def serve():
        conn = listener.accept()
        req = json.loads(conn.recv_bytes().decode().strip())
        conn.send_bytes((json.dumps({"error": "success", "data": 42,
                                     "request_id": req["request_id"]}) + "\n").encode())
        time.sleep(0.5)
        conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        assert ipc.get_property(tmp_path / "pipe-test.sock", "volume", timeout=5) == 42
    finally:
        t.join(5)
        listener.close()
    assert not ipc.endpoint_exists(tmp_path / "pipe-test.sock")


@win_only
@pytest.mark.skipif(not shutil.which(os.environ.get("MEDIA_MPV_BIN", "mpv")),
                    reason="mpv is not installed")
def test_a_real_mpv_over_its_pipe(tmp_path):
    sock = tmp_path / "sink-speech.sock"
    name = ipc.endpoint_name(sock)
    proc = subprocess.Popen([os.environ.get("MEDIA_MPV_BIN", "mpv"), "--idle=yes",
                             "--no-config", "--no-terminal", "--ao=null", "--vo=null",
                             f"--input-ipc-server={name}"])
    try:
        for _ in range(100):
            if ipc.endpoint_exists(sock):
                break
            time.sleep(0.1)
        assert ipc.get_property(sock, "idle-active") is True
        ipc.set_property(sock, "volume", 77)
        assert ipc.get_property(sock, "volume") == 77
        assert ipc.get_properties(sock, ["idle-active", "volume"]) == {
            "idle-active": True, "volume": 77}
        ipc.command(sock, "quit")
        proc.wait(10)
    finally:
        if proc.poll() is None:
            proc.kill()
