"""The per-thread stream, `GET /threads/{session}/events` (server-contract.md §11).

Over real HTTP to an in-process canvas (test_contract's rig), with the
watcher's intervals shrunk so a test takes tenths of a second. The transcript
is a synthetic file (test_transcript's `Script`); speech, panes and ABS are
fakes. Nothing here can reach a pane: `typed` records instead.
"""

from __future__ import annotations

import json
import socket
import time

import pytest

from agent_media_server import auth_abs, sessions, thread_events

from test_contract import AUTH, SID2, call, server, shelf, signed_in, typed  # noqa: F401
from test_transcript import SID, Script


@pytest.fixture()
def fast(monkeypatch):
    monkeypatch.setattr(thread_events, "POLL_S", 0.02)
    monkeypatch.setattr(thread_events, "DEBOUNCE_S", 0.03)
    monkeypatch.setattr(thread_events, "DEBOUNCE_MAX_S", 0.2)
    monkeypatch.setattr(thread_events, "FAST_S", 0.1)
    monkeypatch.setattr(thread_events, "SLOW_S", 0.1)
    monkeypatch.setattr(thread_events, "PING_S", 0.3)


@pytest.fixture()
def spoken(monkeypatch):
    """What speech history says, settable by the test; nothing is working."""
    from agent_media_core import activity, book_tracks

    lines: list = []
    monkeypatch.setattr(book_tracks, "conversation_log",
                        lambda s, f, target=None, positions=True: [dict(l) for l in lines])
    monkeypatch.setattr(activity, "attach", lambda s, ls: None)
    return lines


@pytest.fixture()
def convo(tmp_path, shelf, spoken, fast, monkeypatch):
    """SID has a transcript with one exchange, and is live in pane %7. The
    returned dict is the live sweep's answer; a test changes it to end one."""
    s = Script(tmp_path / "claude" / "projects" / "-w" / f"{SID}.jsonl")
    s.prompt("Hello")
    s.text("Hi there.", msgid="m0")
    s.end_turn()
    live = {SID: "%7", SID2: "%42"}
    monkeypatch.setattr(sessions, "live_sessions", lambda: dict(live))
    return s, live


class Stream:
    """A raw SSE reader: the frames as they arrive, with a deadline."""

    def __init__(self, addr, path, headers=None):
        self.sock = socket.create_connection(addr, timeout=5)
        hdrs = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
        self.sock.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nAccept: text/event-stream\r\n"
                          f"{hdrs}\r\n".encode())
        self.buf = b""
        head = self._until(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        self.status = int(lines[0].split()[1])
        self.headers = {k.lower(): v.strip() for k, _, v in
                        (ln.partition(":") for ln in lines[1:] if ln)}

    def _until(self, sep: bytes, deadline: float = 5.0) -> bytes:
        end = time.monotonic() + deadline
        while sep not in self.buf:
            self.sock.settimeout(max(0.01, end - time.monotonic()))
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                raise TimeoutError(f"no {sep!r} in {self.buf[:200]!r}")
            if not chunk:
                raise EOFError(self.buf[:200])
            self.buf += chunk
        head, _, self.buf = self.buf.partition(sep)
        return head

    def body(self) -> dict:
        n = int(self.headers.get("content-length", "0"))
        while len(self.buf) < n:
            self.buf += self.sock.recv(65536)
        return json.loads(self.buf[:n])

    def event(self, deadline: float = 5.0) -> tuple[str, object]:
        while True:
            frame = self._until(b"\n\n", deadline).decode()
            fields = dict(ln.partition(": ")[::2] for ln in frame.split("\n") if ": " in ln)
            if "event" in fields:
                return fields["event"], json.loads(fields["data"])

    def next(self, name: str, deadline: float = 5.0):
        """The next `name` event, skipping others."""
        end = time.monotonic() + deadline
        while True:
            ev, data = self.event(max(0.05, end - time.monotonic()))
            if ev == name:
                return data

    def close(self):
        self.sock.close()


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_snapshot_first_then_appends_as_the_transcript_grows(server, signed_in, convo):
    s, _live = convo
    st = Stream(server, f"/threads/{SID}/events", AUTH)
    assert st.status == 200
    assert st.headers["content-type"] == "text/event-stream"
    assert st.headers["access-control-allow-origin"] == "*"
    ev, snap = st.event()
    assert ev == "snapshot"
    assert {"session", "lines", "messages", "older", "pending", "working", "approval",
            "suggestion", "recap", "state", "live", "pane", "resumable"} <= set(snap)
    assert [m["role"] for m in snap["messages"]] == ["user", "assistant"]
    assert snap["state"] == "working" and snap["live"] is True and snap["pane"] == "%7"

    t0 = time.monotonic()
    s.prompt("And now?")
    got = st.next("message")
    lag = time.monotonic() - t0
    assert got["op"] == "append" and got["message"]["role"] == "user"
    assert got["message"]["parts"][0]["text"] == "And now?"
    # Well inside a poll interval of the old route (1–15 s).
    assert lag < 1.5

    s.tool("Bash", {"command": "make"}, "t1", msgid="m1")
    reply = st.next("message")
    assert reply["op"] == "append" and reply["message"]["turn"]["running"] is True
    s.result("t1", "built")
    s.text("Built it.", msgid="m1b")
    changed = st.next("message")
    assert changed["op"] == "replace" and changed["message"]["id"] == reply["message"]["id"]
    assert changed["message"]["parts"][0]["status"] == "done"
    assert changed["message"]["parts"][-1] == {"type": "text", "text": "Built it."}
    st.close()


def test_speech_joins_with_a_replace_and_live_events(server, signed_in, convo, spoken):
    from agent_media_server import transcript

    s, _live = convo
    st = Stream(server, f"/threads/{SID}/events", AUTH)
    _, snap = st.event()
    reply = snap["messages"][1]
    assert reply["spoken"] is None
    (key,) = transcript.spoken_keys("Hi there.")
    spoken.append({"who": "agent", "text": "Hi there.", "at": reply["at"] + 3, "key": key,
                   "start": None, "end": None, "live": True, "sentences": ["Hi there."],
                   "sentence": 0, "offsets": [0.0], "elapsed": 0.2,
                   "server_time": time.time(), "delay": 0.0, "paused": False})
    got = st.next("message")
    assert got["op"] == "replace" and got["message"]["spoken"]["key"] == key
    live = st.next("live")
    assert live["id"] == reply["id"] and live["sentence"] == 0
    # It finishes: the line is a history row now, and nothing is live.
    spoken[0] = {k: v for k, v in spoken[0].items()
                 if k not in ("live", "sentences", "sentence", "offsets", "elapsed",
                              "server_time", "delay", "paused")}
    spoken[0]["id"] = 12
    # Both arrive from the same read: the message (its `spoken` changed), then
    # the clock going quiet.
    seen = {}
    while len(seen) < 2:
        ev, data = st.event()
        if ev in ("message", "live"):
            seen[ev] = data
    assert seen["live"] is None
    assert seen["message"]["message"]["spoken"] == {"id": 12, "key": key,
                                                    "at": reply["at"] + 3}
    st.close()


def test_state_and_suggestion_events(server, signed_in, convo, monkeypatch):
    from agent_media_server import panes

    _s, live = convo
    st = Stream(server, f"/threads/{SID}/events", AUTH)
    st.event()
    monkeypatch.setattr(panes, "classify", lambda cap, agent="claude": "input")
    assert st.next("state") == {"state": "waiting", "live": True, "pane": "%7"}
    monkeypatch.setattr(sessions, "_followup", lambda s: {"text": "Ship it?", "key": ""})
    assert st.next("suggestion") == {"text": "Ship it?"}
    live.pop(SID)
    assert st.next("state") == {"state": "ended", "live": False, "pane": None}
    st.close()


def test_reconnect_gets_a_fresh_snapshot(server, signed_in, convo):
    s, _live = convo
    st = Stream(server, f"/threads/{SID}/events", AUTH)
    st.event()
    st.close()
    s.prompt("While you were gone")
    st = Stream(server, f"/threads/{SID}/events", {**AUTH, "Last-Event-ID": "7"})
    ev, snap = st.event()
    assert ev == "snapshot"
    assert snap["messages"][-1]["parts"][0]["text"] == "While you were gone"
    st.close()


def test_one_watcher_per_session_and_it_stops_with_the_last(server, signed_in, convo):
    a = Stream(server, f"/threads/{SID}/events", AUTH)
    a.event()
    b = Stream(server, f"/threads/{SID}/events", AUTH)
    b.event()
    assert thread_events.watching() == {SID: 2}
    a.close()
    assert _wait(lambda: thread_events.watching() == {SID: 1})
    b.close()
    assert _wait(lambda: thread_events.watching() == {})


def test_pings_keep_a_quiet_stream_alive(server, signed_in, convo):
    st = Stream(server, f"/threads/{SID}/events", AUTH)
    st.event()
    assert st.next("ping", deadline=2.0) == {}
    st.close()


def test_caps_are_503(server, signed_in, convo, monkeypatch):
    monkeypatch.setattr(thread_events, "MAX_PER_SESSION", 1)
    a = Stream(server, f"/threads/{SID}/events", AUTH)
    a.event()
    b = Stream(server, f"/threads/{SID}/events", AUTH)
    assert b.status == 503 and b.body()["ok"] is False
    b.close()
    monkeypatch.setattr(thread_events, "MAX_PER_SESSION", 8)
    monkeypatch.setattr(thread_events, "MAX_TOTAL", 1)
    c = Stream(server, f"/threads/{SID2}/events", AUTH)
    assert c.status == 503
    c.close()
    a.close()


def test_auth_and_refusals(server, shelf, signed_in, convo, monkeypatch):
    # The query-string token, for a plain EventSource.
    st = Stream(server, f"/threads/{SID}/events?access_token=tok")
    assert st.status == 200 and st.event()[0] == "snapshot"
    st.close()
    res, obj = call(server, "GET", f"/threads/nope/events", headers=AUTH)
    assert res.status == 400 and obj == {"ok": False, "error": "not a session id"}
    monkeypatch.setattr(sessions, "session_exists", lambda s: False)
    ghost = "11111111-2222-4333-8444-555555555555"
    res, obj = call(server, "GET", f"/threads/{ghost}/events", headers=AUTH)
    assert res.status == 404 and obj["ok"] is False
    monkeypatch.setattr(auth_abs, "abs_identity", lambda bearer: (None, 401))
    res, obj = call(server, "GET", f"/threads/{SID}/events")
    assert res.status == 401 and obj["ok"] is False
    assert res.getheader("Access-Control-Allow-Origin") == "*"
    monkeypatch.setattr(auth_abs, "abs_identity",
                        lambda bearer: ({"username": "guest", "type": "user"}, 200))
    res, obj = call(server, "GET", f"/threads/{SID}/events", headers=AUTH)
    assert res.status == 403
    # None of the refusals subscribed; the one stream that opened is gone
    # once its next write (a ping at most) finds the client gone.
    assert _wait(lambda: thread_events.watching() == {})


def test_a_device_token_opens_it(server, shelf, convo, monkeypatch):
    from agent_media_server import devices

    code, _ = devices.mint_code("test phone")
    got = devices.redeem(code, "", "127.0.0.1")
    monkeypatch.setattr(auth_abs, "abs_identity",
                        lambda bearer: (_ for _ in ()).throw(AssertionError("asked ABS")))
    st = Stream(server, f"/threads/{SID}/events",
                {"Authorization": f"Bearer {got['token']}"})
    assert st.status == 200 and st.event()[0] == "snapshot"
    st.close()


def test_preflight_and_the_token_is_never_logged(server, convo, monkeypatch, capsys):
    res, _ = call(server, "OPTIONS", f"/threads/{SID}/events")
    assert res.status == 204 and res.getheader("Access-Control-Allow-Origin") == "*"
    monkeypatch.setenv("MEDIA_VISUAL_DEBUG", "1")
    call(server, "GET", f"/threads/nope/events?access_token=s3cret-token")
    assert "s3cret-token" not in capsys.readouterr().err


def test_canvas_events_is_untouched(server, convo):
    st = Stream(server, "/events")
    assert st.status == 200
    frame = st._until(b"\n\n")
    frame = st._until(b"\n\n")
    assert json.loads(frame.decode().partition("data: ")[2])["kind"] == "hello"
    assert thread_events.watching() == {}
    st.close()
