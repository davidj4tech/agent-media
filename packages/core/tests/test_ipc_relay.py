"""media-ipc-relay: a local door to a far player with a connection already open.

A client is joined to a spare connection the relay opened in advance, the bytes
pass through untouched both ways, each client gets its own far connection, and
the far side's round trip is published for the breaker (a local connect says
nothing about the link).
"""

import json
import socket
import threading
import time

import pytest

from agent_media_core.entrypoints import ipc_relay
from agent_media_core.sinks import _mpv_ipc as ipc


class _Echo:
    """A line player: answers each line with the same line, counts connects."""

    def __init__(self):
        self.connects = 0
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(16)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.connects += 1
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        with c:
            buf = b""
            while True:
                b = c.recv(4096)
                if not b:
                    return
                buf += b
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    req = json.loads(line)
                    c.sendall((json.dumps({"request_id": req.get("request_id"),
                                           "error": "success",
                                           "data": req["command"][-1]}) + "\n").encode())


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    far = _Echo()
    r = ipc_relay.Relay(("127.0.0.1", 0), ("127.0.0.1", far.port), spares=2, max_age=30)
    threading.Thread(target=r.serve_forever, daemon=True).start()
    for _ in range(100):
        with r._lock:
            if r.server and len(r._spares) == 2 and far.connects == 2:
                break
        time.sleep(0.02)
    yield r, far
    r.stop()


def _call(port, name):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
        s.sendall((json.dumps({"command": ["get_property", name], "request_id": 7}) + "\n").encode())
        return json.loads(s.makefile().readline())


def test_a_client_rides_a_spare_and_gets_its_answer(relay):
    r, far = relay
    before = far.connects
    assert before == 2, "the spares were opened in advance"
    got = _call(r.server.getsockname()[1], "pause")
    assert got == {"request_id": 7, "error": "success", "data": "pause"}


def test_each_client_gets_its_own_far_connection_and_spares_are_refilled(relay):
    r, far = relay
    port = r.server.getsockname()[1]
    for n in range(3):
        assert _call(port, f"p{n}")["data"] == f"p{n}"
    for _ in range(100):
        with r._lock:
            if len(r._spares) == 2:
                break
        time.sleep(0.02)
    assert far.connects >= 5   # 2 spares, then one more per client taken


def test_the_far_round_trip_is_published_for_the_breaker(relay, monkeypatch):
    r, far = relay
    port = r.server.getsockname()[1]
    data = json.loads(ipc_relay.rtt_path(port).read_text())
    assert data["upstream"] == f"127.0.0.1:{far.port}"
    monkeypatch.setattr(ipc, "_relay_cache", {})
    assert ipc._relay_rtt_s(f"tcp://127.0.0.1:{port}") == pytest.approx(data["rtt_s"])
    assert ipc._relay_rtt_s("tcp://p8a:6614") is None


def test_the_breaker_budget_follows_the_far_link_not_the_local_connect(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MEDIA_MPV_SLOW_MS", "1200")
    monkeypatch.setenv("MEDIA_MPV_SLOW_CAP_MS", "3000")
    monkeypatch.setattr(ipc, "_relay_cache", {})
    p = ipc_relay.rtt_path(16614)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"upstream": "p8a:6614", "rtt_s": 0.45}))
    monkeypatch.setattr(ipc, "_connect_s", {"tcp://127.0.0.1:16614": [0.0002]})
    assert ipc._link_slow_s("tcp://127.0.0.1:16614") == pytest.approx(4 * 0.45)
