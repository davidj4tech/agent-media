"""A fire-and-forget control still has to notice a refusal.

`send_nowait` exists because waiting costs real latency — pausing suspends the
phone's audio device for the better part of a second, and the presser should
not wait for that. But discarding the reply made an application-level refusal
indistinguishable from success. The phone lane now ends at an app that answers
a *subset* of mpv's verbs, and it said so, clearly, on every press:

    {"error": "invalid parameter"}

Nothing read it. `media toggle` was a dead key for as long as the app had no
`cycle`, and `<`/`>`/jump for as long again after that. Both were found by
accident, weeks apart, by someone noticing the key did nothing.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from agent_media_core.sinks import _mpv_ipc as ipc


def _fake_mpv(sock_path, reply):
    """A one-shot mpv stub that answers the first command with `reply`."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)

    def serve():
        try:
            conn, _ = srv.accept()
            with conn:
                conn.recv(4096)
                if reply is not None:
                    conn.sendall((json.dumps(reply) + "\n").encode())
                    time.sleep(0.1)
        except OSError:
            pass
        finally:
            srv.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t


@pytest.fixture
def recorded(monkeypatch):
    """Capture what would have gone to `media errors`."""
    rows = []
    monkeypatch.setattr(ipc, "_log_refusal",
                        lambda verb, err, cmd: rows.append((verb, err)))
    return rows


def _settle():
    """The reader runs on a thread, so give it a moment to finish."""
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not any(t.name.startswith("Thread") and t.is_alive()
                   for t in threading.enumerate()):
            break
        time.sleep(0.02)
    time.sleep(0.2)


def test_a_refused_verb_is_recorded(recorded, tmp_path):
    sock = tmp_path / "mpv.sock"
    server = _fake_mpv(sock, {"error": "invalid parameter"})
    ipc.send_nowait(sock, "cycle", "pause")
    server.join(timeout=3)
    _settle()
    assert recorded == [("cycle", "invalid parameter")], (
        "the player refused the keypress and nothing said so")


def test_success_is_not_an_error(recorded, tmp_path):
    sock = tmp_path / "mpv.sock"
    server = _fake_mpv(sock, {"error": "success", "data": None})
    ipc.send_nowait(sock, "set_property", "pause", True)
    server.join(timeout=3)
    _settle()
    assert recorded == []


def test_an_async_event_is_not_mistaken_for_the_answer(recorded, tmp_path):
    """mpv volunteers events on the same connection. Only a reply has
    `error`, and reading an event as one would report failures at random."""
    sock = tmp_path / "mpv.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(
                (json.dumps({"event": "property-change", "name": "pause"})
                 + "\n" + json.dumps({"error": "success"}) + "\n").encode())
            time.sleep(0.1)
        srv.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    ipc.send_nowait(sock, "set_property", "pause", True)
    t.join(timeout=3)
    _settle()
    assert recorded == []


def test_a_player_that_never_answers_is_not_an_error(recorded, tmp_path):
    """Silence is the normal case for a bridge that drops packets, and the
    whole point of not waiting. It must not become a reported fault."""
    sock = tmp_path / "mpv.sock"
    monkey = ipc.REFUSAL_READ_S
    try:
        ipc.REFUSAL_READ_S = 0.3
        server = _fake_mpv(sock, None)
        ipc.send_nowait(sock, "stop")
        server.join(timeout=3)
        time.sleep(0.6)
        assert recorded == []
    finally:
        ipc.REFUSAL_READ_S = monkey


def test_the_caller_does_not_wait_for_the_reply(tmp_path, monkeypatch):
    """The latency promise: reading happens after the call returns."""
    monkeypatch.setattr(ipc, "_log_refusal", lambda *a: None)
    sock = tmp_path / "mpv.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        with conn:
            conn.recv(4096)
            time.sleep(1.0)              # a slow player
            try:
                conn.sendall(b'{"error":"success"}\n')
            except OSError:
                pass

    threading.Thread(target=serve, daemon=True).start()
    t0 = time.monotonic()
    ipc.send_nowait(sock, "set_property", "pause", True)
    assert time.monotonic() - t0 < 0.5, (
        "send_nowait waited for the player; the whole point is that it does not")
