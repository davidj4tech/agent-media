"""`POST /session/stop` (server-contract.md §12, stop.py), over HTTP.

Pane sessions: the screen is faked (`activity_of`), the keys are recorded,
never pressed. Headless sessions: the real sessiond and the fake claude of
test_headless. Speech: the canvas's snapshot is faked (`speech.current_state`)
and stopping it is a recorder.
"""

from __future__ import annotations

import pytest

from agent_media_core.intake import submit as core_submit

from agent_media_server import panes, send, sessions, speech
from agent_media_server.driver import pane as pane_mod
from test_contract import AUTH, call, server, signed_in, typed  # noqa: F401 — fixtures
from test_headless import app_host, host, start, state, wait_for  # noqa: F401 — fixtures

_REAL_RECORD_TURN = send._record_turn   # `typed` replaces it
SID = "6c73498c-02c1-4846-8350-a82006973571"
OTHER = "0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0"


def req(*a, **k):
    res, obj = call(*a, **k)
    return res.status, obj


@pytest.fixture()
def voice(monkeypatch):
    """What is heard (`now`), and a record of every stop."""
    stopped: list = []
    now = {"speaking": False}
    monkeypatch.setattr(speech, "current_state", lambda: dict(now))
    monkeypatch.setattr(speech, "stop_speech", lambda: stopped.append(True) or "")
    return now, stopped


@pytest.fixture()
def pane(monkeypatch, typed):
    """SID live in %42; `screen` is the sequence of states it shows."""
    keys: list = []
    monkeypatch.setattr(panes, "_tmux", lambda argv: keys.append(argv) or "")
    monkeypatch.setattr(panes, "alive", lambda p: True)
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%42"})
    monkeypatch.setattr(sessions, "_agent_of_pane", lambda p: "claude")
    monkeypatch.setattr(pane_mod, "INTERRUPT_WATCH_S", 0.7)
    screen = ["waiting"]

    def activity_of(session, p, **kw):
        if len(screen) > 1:
            return {"state": screen.pop(0)}
        return {"state": screen[0]}

    monkeypatch.setattr(sessions, "activity_of", activity_of)
    return screen, keys


def stop(server, body):
    return req(server, "POST", "/session/stop", body, AUTH)


def test_working_pane_is_interrupted_and_speech_left_alone(server, signed_in, pane, voice):
    screen, keys = pane
    now, stopped = voice
    screen[:] = ["working", "working", "waiting"]
    now.update(speaking=True, session=SID)
    st, body = stop(server, {"session": SID})
    assert st == 200, body
    cutoff = body.pop("cutoff")
    assert body == {"ok": True, "session": SID, "did": "interrupted", "interrupted": True,
                    "why": None, "speech": "left", "flushed": 0, "state": "waiting"}
    assert keys == [["send-keys", "-t", "%42", "Escape"]]
    assert stopped == []
    # The cutoff: this thread's speech submitted after the press is dropped,
    # what came before is not, and no `all` cut was set.
    assert isinstance(cutoff, float)
    assert core_submit.session_speech_cut(SID) == {"after": cutoff}
    assert core_submit._speech_cut(SID, cutoff + 1)
    assert not core_submit._speech_cut(SID, cutoff - 1)
    assert core_submit.session_speech_cut(OTHER) == {}


def test_idle_pane_with_its_speech_playing_stops_the_speech(server, signed_in, pane, voice):
    screen, keys = pane
    now, stopped = voice
    now.update(paused=True, session=SID)
    st, body = stop(server, {"session": SID})
    assert body["did"] == "silenced" and body["speech"] == "stopped"
    assert body["interrupted"] is False and body["why"] == "not working"
    assert keys == [] and stopped == [True]


def test_the_idle_stop_drops_this_threads_queue_too(server, signed_in, pane, voice,
                                                   monkeypatch):
    screen, keys = pane
    now, stopped = voice
    now.update(speaking=True, session=SID)
    monkeypatch.setattr(speech, "waiting_for",
                        lambda s, at: [{"session": s, "at": at - 1}] * 2 if s == SID else [])
    st, body = stop(server, {"session": SID})
    assert body["did"] == "silenced" and body["speech"] == "stopped"
    assert body["flushed"] == 2 and body["cutoff"] is None
    cut = core_submit.session_speech_cut(SID)
    assert set(cut) == {"all"}
    # Everything of this thread's up to the press, nothing after it.
    assert core_submit._speech_cut(SID, cut["all"] - 5)
    assert not core_submit._speech_cut(SID, cut["all"] + 1)
    assert core_submit.session_speech_cut(OTHER) == {}


def test_silence_drops_the_queue_while_another_thread_is_heard(server, signed_in, pane,
                                                               voice, monkeypatch):
    screen, keys = pane
    now, stopped = voice
    now.update(speaking=True, session=OTHER)
    monkeypatch.setattr(speech, "waiting_for", lambda s, at: [{"session": s, "at": at}])
    st, body = stop(server, {"session": SID, "speech": "silence"})
    assert body["did"] == "silenced" and body["speech"] == "stopped" and body["flushed"] == 1
    assert stopped == []                       # the clip heard is not ours
    assert set(core_submit.session_speech_cut(SID)) == {"all"}
    assert core_submit.session_speech_cut(OTHER) == {}


def test_the_cutoff_ends_with_the_listeners_next_turn(server, signed_in, pane, voice,
                                                      monkeypatch):
    screen, keys = pane
    screen[:] = ["working", "working", "waiting"]
    st, body = stop(server, {"session": SID})
    assert body["cutoff"] and "after" in core_submit.session_speech_cut(SID)

    class _NoThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    monkeypatch.setattr(send.threading, "Thread", _NoThread)
    _REAL_RECORD_TURN(SID, "and now this")
    assert core_submit.session_speech_cut(SID) == {}
    assert not core_submit._speech_cut(SID, body["cutoff"] + 1)


def test_no_cutoff_is_left_when_nothing_was_interrupted(server, signed_in, pane, voice):
    screen, keys = pane
    screen[:] = ["working"]
    st, body = stop(server, {"session": SID})
    assert st == 504
    assert core_submit.session_speech_cut(SID) == {}


def test_another_threads_speech_is_never_touched(server, signed_in, pane, voice):
    screen, keys = pane
    now, stopped = voice
    now.update(speaking=True, session=OTHER)
    st, body = stop(server, {"session": SID, "speech": "silence"})
    assert body["did"] == "nothing" and body["speech"] == "other_thread"
    assert stopped == [] and keys == []


def test_a_dialog_is_never_pressed(server, signed_in, pane, voice):
    screen, keys = pane
    screen[:] = ["approval"]
    st, body = stop(server, {"session": SID})
    assert body["did"] == "nothing" and body["why"] == "waiting on a question"
    assert body["speech"] == "idle" and body["state"] == "approval"
    assert keys == []


def test_silence_interrupts_and_stops_this_threads_speech(server, signed_in, pane, voice):
    screen, keys = pane
    now, stopped = voice
    screen[:] = ["working", "working", "waiting"]
    now.update(speaking=True, session=SID)
    st, body = stop(server, {"session": SID, "speech": "silence"})
    assert body["did"] == "both" and body["speech"] == "stopped" and body["interrupted"]
    assert stopped == [True]
    # Both marks: what it says next and what it had waiting.
    assert set(core_submit.session_speech_cut(SID)) == {"after", "all"}
    assert body["cutoff"] == core_submit.session_speech_cut(SID)["after"]


def test_still_working_after_escape_is_504(server, signed_in, pane, voice):
    screen, keys = pane
    screen[:] = ["working"]
    st, body = stop(server, {"session": SID})
    assert st == 504 and body["ok"] is False and body["error"] == "still working after Escape"


def test_not_live_is_nothing(server, signed_in, pane, voice, monkeypatch):
    monkeypatch.setattr(sessions, "live_sessions", lambda: {})
    st, body = stop(server, {"session": SID})
    assert st == 200 and body["did"] == "nothing" and body["why"] == "not live"
    assert body["state"] == "ended"


def test_bad_requests(server, signed_in, pane, voice):
    assert stop(server, {"session": "nope"})[0] == 400
    st, body = stop(server, {"session": SID, "speech": "loud"})
    assert st == 400 and body["error"] == "speech must be auto or silence"
    res, _ = call(server, "OPTIONS", "/session/stop")
    assert res.status == 204 and res.getheader("Access-Control-Allow-Origin") == "*"


def test_no_bearer_is_refused(server, pane, voice, monkeypatch):
    from agent_media_server import auth_abs

    monkeypatch.setattr(auth_abs, "abs_urls", lambda: ["http://abs"])
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: (None, 401))
    res, body = call(server, "POST", "/session/stop", {"session": SID})
    assert res.status == 401 and body["ok"] is False


def test_a_working_headless_session_is_interrupted(app_host, server, signed_in, typed, voice):
    sid = start(app_host, "slow: 20")
    wait_for(lambda: any(e.get("subtype") == "task_started"
                         for e in app_host.sup.sessions[sid].events))
    st, body = stop(server, {"session": sid})
    assert st == 200, body
    assert body["did"] == "interrupted" and body["interrupted"] is True
    assert body["state"] == "waiting" and isinstance(body["cutoff"], float)
    ev = [e for e in app_host.sup.sessions[sid].events if e.get("type") == "result"]
    assert ev[-1]["terminal_reason"] == "aborted_tools"
    assert not [t for t in typed if t[0] in ("_tmux", "panes.send")]
    # Pressed again, idle: nothing.
    st, body = stop(server, {"session": sid})
    assert body["did"] == "nothing" and body["why"] == "not working"


def test_a_headless_session_on_an_approval_is_not_interrupted(app_host, server, signed_in,
                                                              typed, voice):
    sid = start(app_host, "tool: touch x")
    wait_for(lambda: state(app_host, sid) == "approval")
    st, body = stop(server, {"session": sid})
    assert body["did"] == "nothing" and body["why"] == "waiting on a question"
    assert state(app_host, sid) == "approval"
