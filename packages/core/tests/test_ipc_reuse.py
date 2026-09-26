"""Calls within one reply share connections to a remote player.

Over the phone's link a fresh connect is a whole round trip — 0.44s of every
0.87s property read on 21 Sep — and one reply makes dozens of calls to the
same few players. Reuse is only safe with three guards, each tested here:
replies matched by request_id (a reused socket carries broadcast events and
late replies ahead of the answer), a pooled socket that died retried on a
fresh one (not reported to the breaker as a failing endpoint), and reuse
scoped to one reply (a pooled socket nobody reads still fills with events).
"""

import json
import socket
import threading

import pytest

from agent_media_core import _breaker
from agent_media_core.sinks import _mpv_ipc as ipc


class _Player:
    """A line-JSON player on localhost, like mpv or Sasonica's emulator.

    `event_first`: broadcast an event ahead of every reply.
    `no_ids`: reply without echoing request_id (an old or minimal server).
    `close_after`: drop each connection after this many replies.
    `late`: also send a stray reply carrying an id nobody is waiting for.
    """

    def __init__(self, event_first=False, no_ids=False, close_after=None,
                 late=False, stale_one=False):
        self.connects = 0
        self.disconnects = 0
        self._opts = dict(event_first=event_first, no_ids=no_ids,
                          close_after=close_after, late=late,
                          stale_one=stale_one)
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self.endpoint = f"tcp://127.0.0.1:{self._srv.getsockname()[1]}"
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._stop:
            try:
                c, _ = self._srv.accept()
            except OSError:
                return
            self.connects += 1
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        o = self._opts
        buf, served = b"", 0
        try:
            while True:
                chunk = c.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    req = json.loads(line)
                    out = []
                    if o["event_first"]:
                        out.append({"event": "property-change", "name": "pause"})
                    if o["stale_one"]:
                        # A late answer to some earlier call's request 1 —
                        # on its own, ahead of the real reply, the way a late
                        # packet arrives. In one write with the answer, both
                        # would land in a single recv and the answer would
                        # simply overwrite it.
                        c.sendall((json.dumps({"request_id": 1,
                                               "error": "success",
                                               "data": "STALE"}) + "\n").encode())
                        threading.Event().wait(0.1)
                    if o["late"]:
                        out.append({"request_id": -999, "error": "success",
                                    "data": "WRONG"})
                    cmd = req["command"]
                    data = f"value-of-{cmd[1]}" if cmd[0] == "get_property" else None
                    reply = {"error": "success", "data": data}
                    if not o["no_ids"] and "request_id" in req:
                        reply["request_id"] = req["request_id"]
                    out.append(reply)
                    c.sendall(b"".join((json.dumps(m) + "\n").encode() for m in out))
                    served += 1
                    if o["close_after"] and served >= o["close_after"]:
                        c.close()
                        self.disconnects += 1
                        return
        finally:
            try:
                c.close()
            except OSError:
                pass
        self.disconnects += 1

    def close(self):
        self._stop = True
        self._srv.close()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(ipc, "_breaker_until", None)
    monkeypatch.setattr(ipc, "_connect_s", {})
    monkeypatch.setattr(ipc, "_no_ids", set())
    monkeypatch.setattr(ipc, "_pool", {})
    monkeypatch.setattr(ipc, "_reuse_depth", 0)


@pytest.fixture
def player(request):
    p = _Player(**getattr(request, "param", {}))
    yield p
    p.close()


def _reads(ep, n=5):
    return [ipc.get_property(ep, f"p{i}") for i in range(n)]


def test_calls_in_one_reply_share_a_connection(player):
    with ipc.reuse_connections():
        got = _reads(player.endpoint)
    assert got == [f"value-of-p{i}" for i in range(5)]
    assert player.connects == 1


def test_outside_a_reply_every_call_connects_as_before(player):
    _reads(player.endpoint)
    assert player.connects == 5


@pytest.mark.parametrize("player", [{"event_first": True}], indirect=True)
def test_broadcast_events_are_not_taken_for_the_answer(player):
    with ipc.reuse_connections():
        got = _reads(player.endpoint)
    assert got == [f"value-of-p{i}" for i in range(5)]


@pytest.mark.parametrize("player", [{"late": True}], indirect=True)
def test_a_stray_reply_is_not_taken_for_the_answer(player):
    with ipc.reuse_connections():
        got = _reads(player.endpoint)
    assert "WRONG" not in got


def test_batched_reads_share_it_too(player):
    with ipc.reuse_connections():
        for _ in range(3):
            got = ipc.get_properties(player.endpoint, ["pause", "volume"],
                                     slow_s=0, breaker_s=0)
            assert got == {"pause": "value-of-pause",
                           "volume": "value-of-volume"}
    assert player.connects == 1


@pytest.mark.parametrize("player", [{"close_after": 1}], indirect=True)
def test_a_pooled_connection_that_died_is_replaced_quietly(player):
    """The player drops every connection after one reply. Each reuse then
    finds it closed — and must reconnect without reporting the endpoint as
    failing, or one dead socket would breaker off a healthy player."""
    with ipc.reuse_connections():
        got = _reads(player.endpoint, n=4)
    assert got == [f"value-of-p{i}" for i in range(4)]
    assert _breaker.load("mpv") == {}


@pytest.mark.parametrize("player", [{"no_ids": True}], indirect=True)
def test_a_server_that_echoes_no_ids_is_never_pooled(player):
    with ipc.reuse_connections():
        got = _reads(player.endpoint, n=3)
    assert got == [f"value-of-p{i}" for i in range(3)]
    assert player.connects == 3, "nothing to match a reused reply on"


def test_the_pool_is_closed_when_the_reply_ends(player):
    with ipc.reuse_connections():
        _reads(player.endpoint, n=2)
    assert ipc._pool == {}
    # The far side sees the connection go, rather than holding it idle.
    for _ in range(50):
        if player.disconnects:
            break
        threading.Event().wait(0.02)
    assert player.disconnects == 1


def test_an_idle_connection_is_not_trusted(player, monkeypatch):
    """A dozing phone leaves a half-open socket that fails by timeout."""
    monkeypatch.setattr(ipc, "_POOL_IDLE_S", -1)
    with ipc.reuse_connections():
        _reads(player.endpoint, n=3)
    assert player.connects == 3


def test_nested_scopes_keep_the_pool_until_the_outer_one_ends(player):
    with ipc.reuse_connections():
        with ipc.reuse_connections():
            ipc.get_property(player.endpoint, "a")
        assert ipc._pool, "an inner scope ending closed the outer one's pool"
        ipc.get_property(player.endpoint, "b")
    assert player.connects == 1


@pytest.mark.parametrize("player", [{"stale_one": True}], indirect=True)
def test_a_late_reply_numbered_like_ours_is_not_ours(player, monkeypatch):
    """What numbering each batch from 1 would get wrong: a late answer to an
    earlier call's request 1 lands first and is taken for this call's."""
    monkeypatch.setattr(ipc, "_ids", __import__("itertools").count(1000))
    with ipc.reuse_connections():
        got = ipc.get_properties(player.endpoint, ["pause"], slow_s=0,
                                 breaker_s=0)
    assert got == {"pause": "value-of-pause"}


@pytest.mark.parametrize("player", [{"close_after": 1}], indirect=True)
def test_a_socket_that_dies_in_use_is_retried_not_breakered(player, monkeypatch):
    """The peek cannot see every death — a socket can go between the check
    and the send. Force that: trust every pooled socket, and let the player
    drop each one after a reply."""
    monkeypatch.setattr(ipc, "_peer_closed", lambda s: False)
    with ipc.reuse_connections():
        got = _reads(player.endpoint, n=3)
    assert got == [f"value-of-p{i}" for i in range(3)]
    assert _breaker.load("mpv") == {}, (
        "a dead pooled socket was reported as a failing endpoint")


def test_a_batch_in_a_reply_rides_the_open_connection(player):
    """The playlist load and start used to connect afresh each (a round trip,
    and a lost SYN's second) and then wait 300ms before closing."""
    import time
    with ipc.reuse_connections():
        assert ipc.get_property(player.endpoint, "a") == "value-of-a"
        t0 = time.monotonic()
        ipc.command_batch(player.endpoint, [["playlist-clear"],
                                            ["loadfile", "tts:x?text=Hi.", "append"]])
        assert time.monotonic() - t0 < 0.2, "no drain wait on a pooled batch"
        # The batch's answers are still arriving on this connection; the next
        # read must skip them and get its own.
        assert ipc.get_property(player.endpoint, "b") == "value-of-b"
    assert player.connects == 1


def test_a_batch_outside_a_reply_still_connects_and_drains(player):
    ipc.command_batch(player.endpoint, [["playlist-clear"]])
    ipc.command_batch(player.endpoint, [["playlist-clear"]])
    assert player.connects == 2


def test_an_error_answer_is_final_when_asked(monkeypatch):
    calls = []

    def answer(*a, **k):
        calls.append(1)
        return {"error": "property not found"}

    monkeypatch.setattr(ipc, "_send", answer)
    with pytest.raises(ipc.MpvIpcError):
        ipc.get_property("tcp://p8a:6614", "user-data/am-owner", retry_errors=False)
    assert len(calls) == 1
    calls.clear()
    with pytest.raises(ipc.MpvIpcError):
        ipc.get_property("tcp://p8a:6614", "user-data/am-owner")
    assert len(calls) == 3, "the default still retries a tcp endpoint"
