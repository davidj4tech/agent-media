"""The session list as a stream, `GET /sessions/events` (server-contract.md §6.13).

Over real HTTP to an in-process canvas (test_contract's rig), with the
watcher's poll and the ping shrunk. Sessions, panes and ABS are fakes: the
screen's state is a variable the test sets.
"""

from __future__ import annotations

import time

import pytest

from agent_media_server import alerts, auth_abs, panes, session_events, sessions

from test_contract import AUTH, SID2, server, shelf, signed_in, typed  # noqa: F401
from test_thread_events import Stream, _wait


@pytest.fixture()
def screen(shelf, signed_in, monkeypatch):
    """SID2 is live in %42, titled "Sasonica web"; `screen["cls"]` is what
    its pane looks like (panes.classify's answer)."""
    s = {"cls": "working"}
    monkeypatch.setattr(panes, "classify", lambda cap, agent="claude": s["cls"])
    monkeypatch.setattr(sessions, "_STATES_TTL_S", 0.0)
    monkeypatch.setattr(session_events, "POLL_S", 0.05)
    monkeypatch.setattr(session_events, "PING_MIN_S", 0.2)
    # A closed stream is only noticed at its next write; a long ping kept
    # each test's subscriber attached into the next test.
    monkeypatch.setattr(session_events, "PING_DEFAULT_S", 0.3)
    _idle_watcher()
    yield s
    _idle_watcher()


def _idle_watcher(deadline: float = 5.0) -> None:
    """The watcher is one per process: a stream from the test before may not
    have noticed its socket closed yet (a busy machine), and a subscriber
    that finds it still running is handed that test's rows as its first
    frame. Wait for it to wind down, then forget what it read."""
    w = session_events._W
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        with w.cond:
            if w.subs <= 0 and w.thread is None:
                break
        time.sleep(0.02)
    with w.cond:
        w.rows = None
        w.alerts_head = 0


def test_refused_without_a_credential(server, screen, monkeypatch):
    monkeypatch.setattr(auth_abs, "abs_identity", lambda bearer: (None, 401))
    st = Stream(server, "/sessions/events")
    try:
        assert st.status == 401
        assert st.body()["ok"] is False
    finally:
        st.close()


def test_first_frame_is_the_list(server, screen):
    st = Stream(server, "/sessions/events", AUTH)
    try:
        assert st.status == 200
        assert st.headers["content-type"] == "text/event-stream"
        ev, data = st.event()
        assert ev == "sessions"
        assert data["sessions"] == [{"session": SID2, "title": "Sasonica web", "state": "working"}]
        assert isinstance(data["at"], float)
    finally:
        st.close()


def test_a_state_change_sends_the_list_again(server, screen):
    st = Stream(server, "/sessions/events?ping=0.3", AUTH)
    try:
        assert st.next("sessions")["sessions"][0]["state"] == "working"
        screen["cls"] = "input"  # the turn ended: waiting on you
        assert st.next("sessions")["sessions"][0]["state"] == "waiting"
        screen["cls"] = "approval"
        assert st.next("sessions")["sessions"][0]["state"] == "approval"
    finally:
        st.close()


def test_nothing_changed_sends_only_pings(server, screen):
    st = Stream(server, "/sessions/events?ping=0.2", AUTH)
    try:
        assert st.event()[0] == "sessions"
        assert st.event(2.0) == ("ping", {})
        assert st.event(2.0) == ("ping", {})
    finally:
        st.close()


def test_access_token_in_the_query(server, screen):
    st = Stream(server, "/sessions/events?access_token=tok")
    try:
        assert st.status == 200
        assert st.event()[0] == "sessions"
    finally:
        st.close()


def test_over_the_cap_is_503(server, screen, monkeypatch):
    monkeypatch.setattr(session_events, "MAX_TOTAL", 1)
    a = Stream(server, "/sessions/events", AUTH)
    try:
        assert a.event()[0] == "sessions"
        b = Stream(server, "/sessions/events", AUTH)
        try:
            assert b.status == 503
            assert b.body()["error"] == "too many open streams"
        finally:
            b.close()
    finally:
        a.close()


def test_the_watcher_stops_when_the_last_goes(server, screen):
    st = Stream(server, "/sessions/events?ping=0.2", AUTH)
    assert st.event()[0] == "sessions"
    assert session_events._W.thread is not None
    st.close()
    # The handler notices at its next write (a ping), then the watcher ends.
    assert _wait(lambda: session_events._W.thread is None and session_events._W.subs == 0)


def test_a_revoked_credential_ends_the_stream(server, screen, monkeypatch):
    monkeypatch.setattr(session_events, "AUTH_RECHECK_S", 0.1)
    st = Stream(server, "/sessions/events?ping=0.2", AUTH)
    try:
        assert st.event()[0] == "sessions"
        monkeypatch.setattr(auth_abs, "abs_identity", lambda bearer: (None, 401))
        with pytest.raises(EOFError):
            for _ in range(20):
                st.event(2.0)
    finally:
        st.close()


def test_ping_is_clamped():
    assert session_events.ping_of("") == session_events.PING_DEFAULT_S
    assert session_events.ping_of("abc") == session_events.PING_DEFAULT_S
    assert session_events.ping_of("nan") == session_events.PING_DEFAULT_S
    assert session_events.ping_of("1") == session_events.PING_MIN_S
    assert session_events.ping_of("120") == 120.0
    assert session_events.ping_of("9999") == session_events.PING_MAX_S


def test_rows_carry_three_fields_in_a_stable_order():
    rows = [{"session": "b", "title": "B", "state": "working", "mem_mb": 3, "tail": "x"},
            {"session": "a", "title": None, "state": None},
            {"session": "", "state": "working"}]
    assert session_events.rows_of(rows) == [
        {"session": "a", "title": "", "state": "waiting"},
        {"session": "b", "title": "B", "state": "working"}]


# --- alerts ---------------------------------------------------------------------------

def _raise(title="red5: / at 91% (7G free)", level="warn", step=90):
    ok, d = alerts.report({"id": "disk.red5.root", "level": level, "title": title,
                           "step": step})
    assert ok, d


def test_alerts_only_when_asked(server, screen):
    _raise()
    st = Stream(server, "/sessions/events?ping=0.2", AUTH)
    try:
        assert st.event()[0] == "sessions"
        assert st.event(2.0) == ("ping", {})
    finally:
        st.close()


def test_a_first_connection_is_handed_the_head_not_the_backlog(server, screen):
    _raise()
    st = Stream(server, "/sessions/events?ping=0.3&alerts=", AUTH)
    try:
        assert st.event()[0] == "sessions"
        assert st.next("alerts") == {"last": alerts.last_seq(), "notices": []}
    finally:
        st.close()


def test_a_raise_while_connected_is_a_notice(server, screen):
    st = Stream(server, "/sessions/events?ping=0.3&alerts=0", AUTH)
    try:
        assert st.next("alerts") == {"last": 0, "notices": []}
        _raise()
        got = st.next("alerts")
        assert [(n["id"], n["change"], n["title"]) for n in got["notices"]] == [
            ("disk.red5.root", "raised", "red5: / at 91% (7G free)")]
        assert got["last"] == alerts.last_seq()
    finally:
        st.close()


def test_a_reconnect_catches_up_from_its_cursor(server, screen):
    _raise()
    cursor = alerts.last_seq()
    _raise(title="red5: / at 96%", step=95)
    st = Stream(server, "/sessions/events?ping=0.3&alerts=%d" % cursor, AUTH)
    try:
        got = st.next("alerts")
        assert [(n["change"], n["title"]) for n in got["notices"]] == [
            ("escalated", "red5: / at 96%")]
    finally:
        st.close()


def test_alerts_cursor_parsing():
    assert session_events.alerts_of(None) is None
    assert session_events.alerts_of("") == session_events.FIRST
    assert session_events.alerts_of("x") == session_events.FIRST
    assert session_events.alerts_of("-3") == 0
    assert session_events.alerts_of("42") == 42
