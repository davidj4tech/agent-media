"""The session list as a stream, `GET /sessions/events` (server-contract.md §6.13).

Over real HTTP to an in-process canvas (test_contract's rig), with the
watcher's poll and the ping shrunk. Sessions, panes and ABS are fakes: the
screen's state is a variable the test sets.
"""

from __future__ import annotations

import time

import pytest

from agent_media_server import alerts, auth_abs, mic, panes, session_events, sessions

from test_contract import AUTH, SID2, call, server, shelf, signed_in, typed  # noqa: F401
from test_mic import _device
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
        w.mic_head = 0


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


def test_a_waiting_row_carries_its_question(server, screen, monkeypatch):
    """Needs you with the answers: the dialog, trimmed, rides the row; a
    new question (a new key) sends the list again though the state stays."""
    from agent_media_server import dashboard

    dialog = {"question": "Run the tests?", "key": "k1", "agent": "claude",
              "options": [{"n": 1, "label": "Yes", "detail": ""},
                          {"n": 2, "label": "No", "detail": "tell Claude"}]}
    monkeypatch.setattr(dashboard, "_approval",
                        lambda sid, live, headless: dict(dialog) if sid == SID2 else None)
    st = Stream(server, "/sessions/events?ping=0.3", AUTH)
    try:
        assert "approval" not in st.next("sessions")["sessions"][0]
        screen["cls"] = "approval"
        row = st.next("sessions")["sessions"][0]
        assert row["approval"] == {
            "key": "k1", "kind": "tool", "question": "Run the tests?",
            "options": [{"n": 1, "label": "Yes"}, {"n": 2, "label": "No"}],
            "multiSelect": False, "partial": False, "several": False}
        dialog["key"] = "k2"
        assert st.next("sessions")["sessions"][0]["approval"]["key"] == "k2"
    finally:
        st.close()


def test_brief_marks_what_one_tap_cannot_answer():
    ap = {"id": "r1", "kind": "question", "key": "abc", "question": "Which?",
          "options": [], "partial": True, "multiSelect": True,
          "questions": [{"question": "Which?"}, {"question": "And?"}]}
    b = session_events.brief(ap)
    assert (b["kind"], b["several"], b["multiSelect"], b["partial"]) == \
        ("question", True, True, True)
    assert b["options"] == []


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
    # Through a tunnel: under Cloudflare's 100 s idle cut.
    assert session_events.ping_of("120", proxied=True) == session_events.PING_PROXIED_MAX_S
    assert session_events.ping_of("30", proxied=True) == 30.0


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


# --- mic: asks to speak for another device (§6.20) ----------------------------------

def test_mic_only_when_asked(server, screen):
    tv, _ = _device("Living room TV")
    assert call(server, "POST", "/mic/ask", {}, tv)[0].status == 200
    st = Stream(server, "/sessions/events?ping=0.2", AUTH)
    try:
        assert st.event()[0] == "sessions"
        assert st.event(2.0) == ("ping", {})
    finally:
        st.close()


def test_an_open_ask_follows_the_first_list(server, screen):
    tv, tv_id = _device("Living room TV")
    phone, _ = _device("Pixel 8a")
    a = call(server, "POST", "/mic/ask", {"session": SID2}, tv)[1]["ask"]
    st = Stream(server, "/sessions/events?ping=0.3&mic=1", phone)
    try:
        assert st.event()[0] == "sessions"
        ev, data = st.event()
        assert ev == "mic"
        assert data == {"asks": [a]}
        assert a["device_id"] == tv_id and a["title"] == "Sasonica web"
    finally:
        st.close()


def test_no_asks_on_connecting_sends_no_mic_frame(server, screen):
    phone, _ = _device("Pixel 8a")
    st = Stream(server, "/sessions/events?ping=0.2&mic=1", phone)
    try:
        assert st.event()[0] == "sessions"
        assert st.event(2.0) == ("ping", {})
    finally:
        st.close()


def test_an_ask_reaches_the_phone_at_once_and_a_cancel_takes_it_down(server, screen,
                                                                     monkeypatch):
    tv, _ = _device("Living room TV")
    phone, _ = _device("Pixel 8a")
    st = Stream(server, "/sessions/events?ping=0.3&mic=1", phone)
    try:
        assert st.event()[0] == "sessions"
        # The watcher's tick is now far off: only the ask's own poke can
        # bring the frame in time.
        monkeypatch.setattr(session_events, "POLL_S", 30.0)
        a = call(server, "POST", "/mic/ask", {}, tv)[1]["ask"]
        assert st.next("mic", 2.0) == {"asks": [a]}
        assert call(server, "POST", "/mic/cancel", {"id": a["id"]}, phone)[0].status == 200
        assert st.next("mic", 2.0) == {"asks": []}
    finally:
        st.close()


def test_a_devices_own_ask_is_not_sent_to_it(server, screen):
    tv, _ = _device("Living room TV")
    st = Stream(server, "/sessions/events?ping=0.2&mic=1", tv)
    try:
        assert st.event()[0] == "sessions"
        assert call(server, "POST", "/mic/ask", {}, tv)[0].status == 200
        assert st.event(2.0) == ("ping", {})
    finally:
        st.close()


def test_an_expiry_is_noticed_by_the_watcher(server, screen, monkeypatch):
    tv, _ = _device("Living room TV")
    phone, _ = _device("Pixel 8a")
    a = call(server, "POST", "/mic/ask", {}, tv)[1]["ask"]
    st = Stream(server, "/sessions/events?ping=0.3&mic=1", phone)
    try:
        assert st.next("mic") == {"asks": [a]}
        later = a["expires"] + 1
        monkeypatch.setattr(mic.time, "time", lambda: later)
        assert st.next("mic", 2.0) == {"asks": []}
    finally:
        st.close()


def test_an_agents_ask_of_the_phone_reaches_a_phone_that_can(server, screen, monkeypatch):
    """`?phone=photo`: the stream is what lets an agent's ask wait for an
    answer (none open: `no_phone`), and the ask arrives at once (§6.21)."""
    from agent_media_server import phone as phone_asks

    monkeypatch.setenv("AMUX_AUTH_TOKEN", "hosttok")
    host = {"X-Auth-Token": "hosttok"}
    pixel, _ = _device("Pixel 8a")
    body = {"kind": "photo", "why": "show me the router lights"}
    assert call(server, "POST", "/phone/ask", body, host)[1]["ask"]["status"] == "no_phone"
    st = Stream(server, "/sessions/events?ping=0.3&phone=photo,bogus", pixel)
    try:
        assert st.event()[0] == "sessions"
        monkeypatch.setattr(session_events, "POLL_S", 30.0)
        a = call(server, "POST", "/phone/ask", body, host)[1]["ask"]
        assert a["status"] == "open"
        assert st.next("phone", 2.0) == {"asks": [a]}
        call(server, "POST", "/phone/answer", {"id": a["id"], "decision": "deny"}, pixel)
        assert st.next("phone", 2.0) == {"asks": []}
    finally:
        st.close()
    assert phone_asks.open_asks(("photo",)) == []


def test_a_catchup_reaches_a_phone_that_asked(server, screen, monkeypatch):
    """`?catchup=1`: each catch-up made is a `catchup` frame, at once (§6.22)."""
    from agent_media_core import catchup
    from agent_media_core.intake import _summary

    monkeypatch.setattr(catchup, "_LAST", None)
    monkeypatch.setattr(catchup, "_listeners", [session_events.poke])
    monkeypatch.setattr(catchup, "_say", lambda payload: None)
    monkeypatch.setattr(_summary, "_chat", lambda *a, **k: None)
    pixel, _ = _device("Pixel 8a")
    st = Stream(server, "/sessions/events?ping=0.3&catchup=1", pixel)
    try:
        assert st.event()[0] == "sessions"
        monkeypatch.setattr(session_events, "POLL_S", 30.0)
        rec = catchup.deliver([{"id": 1, "kind": "reply", "thread": "radio", "text": "a"}],
                              {"call"}, time.time())
        got = st.next("catchup", 2.0)
        assert got == rec and got["replies"] == 1 and got["why"] == ["call"]
    finally:
        st.close()


def test_the_free_frame_follows_busy(server, screen, monkeypatch):
    """`?free=1`: the answer on connecting, and again when the phone reports busy."""
    pixel, _ = _device("Pixel 8a")
    st = Stream(server, "/sessions/events?ping=0.3&free=1", pixel)
    try:
        assert st.next("free", 2.0)["free"] is True
        monkeypatch.setattr(session_events, "POLL_S", 30.0)
        call(server, "POST", "/device/state", {"quiet": True}, pixel)
        got = st.next("free", 2.0)
        assert got["free"] is False and got["why"] == ["quiet"] and got["held"] == 0
    finally:
        st.close()
