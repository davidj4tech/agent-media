"""Speech on the phone as frames (speech_frames.py, roadmap item 15).

The callers' side is real mpv JSON-IPC over a loopback socket, as
`sinks/speech.py` sends it; the phone's side is a paired device's
`/sessions/events?speech=frames` stream and its `POST /speech/state`.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from agent_media_server import session_events, speech_frames

from test_contract import AUTH, SID2, call, server, shelf, signed_in, typed  # noqa: F401
from test_mic import _device
from test_session_events import screen  # noqa: F401
from test_thread_events import Stream, _wait


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    speech_frames._reset_for_tests()
    yield
    speech_frames._reset_for_tests()


@pytest.fixture()
def endpoint():
    srv = speech_frames.start("127.0.0.1:0", upstream="")
    yield srv.getsockname()
    srv.close()


class Ipc:
    """A caller, as `_mpv_ipc` is one: a line out, the answer back."""

    def __init__(self, addr):
        self.sock = socket.create_connection(addr, timeout=5)
        self.buf = b""
        self.n = 0

    def line(self) -> dict:
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def __call__(self, *cmd):
        self.n += 1
        self.sock.sendall((json.dumps({"command": list(cmd), "request_id": self.n}) + "\n").encode())
        while True:
            msg = self.line()
            if msg.get("request_id") == self.n:
                return msg

    def close(self):
        self.sock.close()


def _listening(device="d_test"):
    return speech_frames.listening(device, True)


# --- the protocol, answered here ----------------------------------------------


def test_claim_play_is_one_frame_and_reads_are_local(endpoint):
    _listening()
    ipc = Ipc(endpoint)
    mine = {"owner": "reply-1", "deadline": 2000}
    got = ipc("am-claim-play", "user-data/am-owner", mine, 1000, [
        ["set_property", "audio-device", "default"],
        ["stop"], ["playlist-clear"],
        ["loadfile", "tts:a?text=One.", "append"],
        ["loadfile", "tts:b?text=Two.", "append"],
        ["set_property", "pause", False],
        ["set_property", "playlist-pos", 0],
    ])
    assert got == {"data": mine, "error": "success", "request_id": 1}
    frames = speech_frames.frames_after(0)
    assert len(frames) == 1
    assert [o["op"] for o in frames[0]["ops"]] == [
        "stop", "clear", "load", "load", "pause", "pos"]
    # Read back at once, with no phone in the loop.
    assert ipc("get_property", "playlist-count")["data"] == 2
    assert ipc("get_property", "playlist-pos")["data"] == 0
    assert ipc("get_property", "idle-active")["data"] is False
    assert ipc("get_property", "path")["data"] == "tts:a?text=One."
    ipc.close()


def test_someone_elses_claim_holds_and_its_commands_do_not_run(endpoint):
    _listening()
    ipc = Ipc(endpoint)
    theirs = {"owner": "book", "deadline": 5000}
    ipc("am-claim", "user-data/am-owner", theirs, 1000)
    got = ipc("am-claim-play", "user-data/am-owner", {"owner": "reply", "deadline": 2000}, 1000,
              [["stop"], ["loadfile", "tts:x", "append"]])
    assert got["data"] == theirs
    assert speech_frames.frames_after(0) == []


def test_set_get_seek_and_metadata(endpoint):
    _listening()
    ipc = Ipc(endpoint)
    ipc("loadfile", "tts:a", "replace")
    assert ipc("set_property", "user-data/agent-media/speaking", True)["error"] == "success"
    assert ipc("set_property", "force-media-title", "A reply")["error"] == "success"
    assert ipc("get_property", "media-title")["data"] == "A reply"
    assert ipc("set_property", "no-such-thing", 1)["error"] == "property not found"
    assert ipc("set_property", "gapless-audio", "yes")["error"] == "success"
    ipc("seek", 3, "absolute")
    ops = [o for f in speech_frames.frames_after(0) for o in f["ops"]]
    assert {"op": "meta", "name": "user-data/agent-media/speaking", "value": True} in ops
    assert {"op": "seek", "t": 3.0} in ops
    # gapless-audio is accepted and goes nowhere.
    assert not any(o.get("name") == "gapless-audio" for o in ops)
    assert ipc("get_property", "time-pos")["data"] >= 3.0
    assert ipc("get_property_string", "pause")["data"] == "no"


def test_a_phone_report_wins_but_old_news_moves_only_the_playhead(endpoint):
    _listening()
    ipc = Ipc(endpoint)
    for uri in ("tts:a", "tts:b", "tts:c"):
        ipc("loadfile", uri, "append-play")
    s = speech_frames.seq()
    ok, _ = speech_frames.report("d_test", {"seq": s, "pos": 1, "count": 3, "paused": False,
                                            "idle": False, "time_pos": 1.5, "duration": 4.0,
                                            "ringer": {"mode": "normal"}})
    assert ok
    assert ipc("get_property", "playlist-pos")["data"] == 1
    assert ipc("get_property", "user-data/agent-media/ringer")["data"] == {"mode": "normal"}
    # A jump made here, then a report from before the phone saw it.
    ipc("set_property", "playlist-pos", 2)
    speech_frames.report("d_test", {"seq": s, "pos": 1, "count": 3, "time_pos": 2.0})
    assert ipc("get_property", "playlist-pos")["data"] == 2
    # The phone ended it on its own: idle, and the follower sees the end.
    speech_frames.report("d_test", {"seq": speech_frames.seq(), "pos": -1, "count": 0,
                                    "idle": True, "eof": True})
    assert ipc("get_property", "idle-active")["data"] is True
    assert ipc("get_property", "eof-reached")["data"] is True
    assert ipc("get_property", "playlist-count")["data"] == 0


def test_only_the_playing_device_reports():
    _listening("d_phone")
    ok, detail = speech_frames.report("d_tv", {"seq": 0})
    assert not ok and detail["status"] == 409


def test_observers_hear_a_report(endpoint):
    _listening()
    ipc = Ipc(endpoint)
    ipc("loadfile", "tts:a", "replace")
    ipc("loadfile", "tts:b", "append")
    ipc("observe_property", 7, "playlist-pos")
    assert ipc.line() == {"event": "property-change", "id": 7, "name": "playlist-pos", "data": 0}
    speech_frames.report("d_test", {"seq": speech_frames.seq(), "pos": 1, "count": 2})
    assert ipc.line() == {"event": "property-change", "id": 7, "name": "playlist-pos", "data": 1}


def test_no_frame_device_passes_through_to_the_phone():
    far = socket.socket()
    far.bind(("127.0.0.1", 0))
    far.listen(2)

    def echo():
        c, _ = far.accept()
        c.sendall(c.recv(1024).replace(b'"request_id": 1}', b'"request_id": 1, "far": true}'))
        c.close()

    threading.Thread(target=echo, daemon=True).start()
    srv = speech_frames.start("127.0.0.1:0", upstream="127.0.0.1:%d" % far.getsockname()[1])
    try:
        ipc = Ipc(srv.getsockname())
        assert ipc("client_name").get("far") is True
        ipc.close()
        # A caller that half-closes (nc, a one-shot script) still gets its answer.
        threading.Thread(target=echo, daemon=True).start()
        c = socket.create_connection(srv.getsockname(), timeout=5)
        c.sendall(b'{"command": ["client_name"], "request_id": 1}\n')
        c.shutdown(socket.SHUT_WR)
        assert b'"far": true' in c.recv(1024)
        c.close()
    finally:
        srv.close()
        far.close()


# --- the stream ------------------------------------------------------------------


def test_frames_go_down_the_devices_stream_at_once(server, screen, endpoint, monkeypatch):
    pixel, dev = _device("Pixel 8a")
    st = Stream(server, "/sessions/events?ping=0.3&speech=frames", pixel)
    try:
        assert st.event()[0] == "sessions"
        assert _wait(lambda: speech_frames._HUBS["speech"].device() == dev)
        monkeypatch.setattr(session_events, "POLL_S", 30.0)
        ipc = Ipc(endpoint)
        t0 = time.monotonic()
        ipc("loadfile", "tts:a?text=Hello.", "replace")
        f = st.next("speech", 2.0)
        assert time.monotonic() - t0 < 1.0
        assert f["ops"] == [{"op": "load", "uri": "tts:a?text=Hello.", "mode": "replace"}]
        # The phone's report, by its own token.
        r = call(server, "POST", "/speech/state",
                 {"seq": f["seq"], "pos": 0, "count": 1, "idle": False, "time_pos": 0.4}, pixel)
        assert r[0].status == 200 and r[1]["ok"]
        assert ipc("get_property", "time-pos")["data"] >= 0.4
        ipc.close()
    finally:
        st.close()
    assert _wait(lambda: speech_frames._HUBS["speech"].device() is None)


def test_a_reconnecting_stream_gets_what_it_missed(server, screen, endpoint):
    pixel, _ = _device("Pixel 8a")
    _listening("someone")  # frames are made while the phone is away
    ipc = Ipc(endpoint)
    ipc("loadfile", "tts:a", "replace")
    ipc("loadfile", "tts:b", "append")
    first = speech_frames.frames_after(0)[0]["seq"]
    st = Stream(server, f"/sessions/events?ping=0.3&speech=frames&speech_after={first}", pixel)
    try:
        assert st.next("speech", 2.0)["ops"][0]["uri"] == "tts:b"
    finally:
        st.close()


def test_state_needs_a_device(server, screen):
    assert call(server, "POST", "/speech/state", {"seq": 0}, AUTH)[0].status == 401


def test_music_is_its_own_channel(server, screen, monkeypatch):
    """The music player's frames go out as `music`, its reads are its own,
    and its state comes back to /music/state."""
    srv = speech_frames.start("127.0.0.1:0", upstream="", channel="music")
    pixel, dev = _device("Pixel 8a")
    st = Stream(server, "/sessions/events?ping=0.3&music=frames", pixel)
    try:
        assert st.event()[0] == "sessions"
        assert _wait(lambda: speech_frames._HUBS["music"].device() == dev)
        assert speech_frames._HUBS["speech"].device() is None
        ipc = Ipc(srv.getsockname())
        ipc("loadfile", "http://localhost:6616/mix.webm", "replace")
        ipc("set_property", "user-data/agent-media/art", "https://x/art.jpg")
        f = st.next("music", 2.0)
        assert f["ops"][0]["uri"] == "http://localhost:6616/mix.webm"
        assert speech_frames.frames_after(0, "speech") == []
        r = call(server, "POST", "/music/state",
                 {"seq": speech_frames.seq("music"), "pos": 0, "count": 1,
                  "time_pos": 61.0, "duration": 3600.0}, pixel)
        assert r[0].status == 200
        assert ipc("get_property", "time-pos")["data"] >= 61.0
        # Not the speech player's report.
        assert call(server, "POST", "/speech/state", {"seq": 0}, pixel)[0].status == 409
        ipc.close()
    finally:
        st.close()
        srv.close()
