"""Draining and resuming around a restart of media-sessiond (19 Oct 2026).

While a restart is pending (core restart_services' record) sessiond holds new
messages, refuses new chats and delivers what it held after; a stop notes
what each live session was doing, and the next start resumes the ones cut
off, once, with a note. Against the fake `claude` (test_headless.py's
`host`: sessiond in-process on a throwaway socket, a throwaway state dir).
Nothing here reaches a real session.
"""

from __future__ import annotations

import json
import time

import pytest

from agent_media_core import restart_services as rs
from agent_media_server import driver, sessiond

from test_headless import _task, host, start, starts, state, wait_for  # noqa: F401


def _pend(**kw) -> dict:
    now = time.time()
    rec = {"id": "r1", "since": now, "deadline": now + 600, "alive_until": now + 600,
           "only": ["agent-media-sessiond"], "state": "waiting", "waiting_on": [], **kw}
    rs._update_pending(lambda r: rec)
    return rec


def _fresh(sup) -> None:
    sup._restart_read_at = None          # past its one-second cache


def _said(sup, sid) -> str:
    return next((e.get("result") for e in reversed(sup.sessions[sid].events)
                 if e.get("type") == "result"), "")


def test_messages_are_held_while_a_restart_is_pending_then_delivered(host):
    sid = start(host, "reply: a")
    wait_for(lambda: _said(host.sup, sid) == "a")
    _pend()
    _fresh(host.sup)
    ok, d = driver.headless_driver().send(sid, "", "reply: held")
    assert ok and d["held"] is True
    time.sleep(0.3)
    assert _said(host.sup, sid) == "a" and state(host, sid) == "waiting"
    assert [m["text"] for m in sessiond.read_record(sid)["held"]] == ["reply: held"]
    assert host.sup.deliver_due() == []
    # No new chat while it is pending.
    ok, d = driver.headless_driver().start(agent="claude", cwd=str(host.work), text="hi")
    assert not ok and d["code"] == "restart_pending"
    # It gave up (or was cancelled): what it held goes out, in order.
    rs.clear_pending()
    _fresh(host.sup)
    assert host.sup.deliver_due() == [sid]
    wait_for(lambda: _said(host.sup, sid) == "held")
    assert sessiond.read_record(sid)["held"] == []


def test_another_sessions_mail_is_not_held_over_a_card(host):
    sid = start(host, "tool: touch q")
    wait_for(lambda: state(host, sid) == "approval")
    _pend()
    _fresh(host.sup)
    with pytest.raises(sessiond.Refused) as e:
        host.sup.send(sid, "[From X] hi", keep_pending=True)
    assert e.value.code == "pending"


def test_held_messages_outlive_the_restart(host):
    sid = start(host, "reply: a")
    wait_for(lambda: _said(host.sup, sid) == "a")
    _pend()
    _fresh(host.sup)
    host.sup.send(sid, "reply: after")
    rs._update_pending(lambda r: {**r, "state": "firing"})
    sessiond.stop(host.srv)
    wait_for(lambda: not host.sup.sessions[sid].live)
    sup2 = sessiond.Supervisor()
    sup2.load()
    try:
        assert rs.read_pending() is None           # the firing restart was this one
        assert [m["text"] for m in sup2.sessions[sid].held] == ["reply: after"]
        assert sup2.deliver_due() == [sid]
        wait_for(lambda: _said(sup2, sid) == "after")
        assert "--resume" in starts(host)[-1]["argv"]
    finally:
        sup2.shutdown()


def test_a_session_cut_off_mid_turn_is_resumed_once_and_an_idle_one_is_not(host):
    busy = start(host, "slow: 20")
    idle = start(host, "reply: b")
    wait_for(lambda: state(host, busy) == "working" and _said(host.sup, idle) == "b")
    _task(host, busy, "task_started", description="Rebuild the index",
          task_type="local_agent", is_backgrounded=True)
    sessiond.stop(host.srv)
    wait_for(lambda: not host.sup.sessions[busy].live)
    cut = sessiond.read_record(busy)["cut_off"]
    assert cut["why"] == "restart" and cut["turn"] is True
    assert "Rebuild the index" in [t["description"] for t in cut["tasks"]]
    assert sessiond.read_record(idle)["cut_off"] is None
    n = len(starts(host))
    sup2 = sessiond.Supervisor()
    sup2.load()
    try:
        assert sup2.sessions[busy].cut_off["due"] is True
        assert sup2.sessions[idle].cut_off is None
        note = sessiond.resume_note(sup2.sessions[busy], sup2.sessions[busy].cut_off)
        assert sup2.deliver_due() == [busy]
        assert sup2.sessions[busy].last_text == " ".join(note.split())[:300]
        assert note.startswith("[From the session host] The session host restarted while "
                               "you were working")
        assert "slow: 20" in note and "1 background agent" in note
        # Continued from its own transcript, never started again.
        assert "SendMessage to its id" in note and "don't start a new one" in note
        assert "\u201cRebuild the index\u201d → a3f6" in note and "tell David" in note
        assert [s["argv"][-1] for s in starts(host)[n:]] == [busy]   # the idle one sleeps
        assert sessiond.read_record(busy)["cut_off"] is None
        assert sup2.deliver_due() == [] and len(starts(host)) == n + 1   # never twice
    finally:
        sup2.shutdown()


def test_a_question_only_session_is_resumed_to_ask_again(host):
    sid = start(host, "tool: touch q")
    wait_for(lambda: state(host, sid) == "approval")
    sessiond.stop(host.srv)
    wait_for(lambda: not host.sup.sessions[sid].live)
    sup2 = sessiond.Supervisor()
    sup2.load()
    try:
        cut = sup2.sessions[sid].cut_off
        assert cut["question"] and not cut["turn"] and cut["due"]
        note = sessiond.resume_note(sup2.sessions[sid], cut)
        assert "waiting for David's answer" in note and "ask it again" in note
    finally:
        sup2.shutdown()


def _record(sid, cwd, **kw):
    root = sessiond.state_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{sid}.json").write_text(json.dumps({"session": sid, "cwd": cwd, **kw}))


def test_a_crashed_host_resumes_from_what_the_record_says(host):
    sid = "6a1e0000-0000-4000-8000-000000000001"
    _record(sid, str(host.work), state="working", last_text="fix the build",
            task_list=[{"id": "b1", "description": "npm test", "type": "local_bash"}],
            tasks=1)
    sup = sessiond.Supervisor()
    sup.load()
    cut = sup.sessions[sid].cut_off
    assert cut["why"] == "crash" and cut["due"] and cut["turn"]
    note = sessiond.resume_note(sup.sessions[sid], cut)
    assert "fix the build" in note and "1 background command" in note
    assert "can't be resumed" in note and "npm test" in note
    assert "SendMessage" not in note   # a command is re-run, not continued


def test_each_stopped_agent_is_named_with_how_to_continue_it(host):
    sid = "6a1e0000-0000-4000-8000-000000000009"
    _record(sid, str(host.work), state="working", last_text="ship it", tasks=3,
            task_list=[{"id": "a1b2c3", "description": "Write the docs", "type": "local_agent"},
                       {"id": "d4e5f6", "description": "Run the suites", "type": "local_agent"},
                       {"id": "b9", "description": "pnpm build", "type": "local_bash"}])
    sup = sessiond.Supervisor()
    sup.load()
    note = sessiond.resume_note(sup.sessions[sid], sup.sessions[sid].cut_off)
    assert "2 background agents" in note and "1 background command" in note
    assert "\u201cWrite the docs\u201d → a1b2c3" in note
    assert "\u201cRun the suites\u201d → d4e5f6" in note
    assert note.index("SendMessage") < note.index("pnpm build")


def test_an_agent_that_ended_by_itself_is_resumed_only_if_the_stop_killed_it(host):
    now = time.time()
    old, raced = ("6a1e0000-0000-4000-8000-000000000002",
                  "6a1e0000-0000-4000-8000-000000000003")
    for sid, at in ((old, now - 3600), (raced, now - 2)):
        _record(sid, str(host.work), state="ended",
                cut_off={"why": "exit", "at": at, "turn": True, "question": False,
                         "tasks": [], "queued": 0, "last_text": "x"})
    sessiond._write_atomic(sessiond.state_root() / "sessiond.stopped.json", {"at": now})
    sup = sessiond.Supervisor()
    sup.load()
    assert sup.sessions[old].cut_off is None
    assert sup.sessions[raced].cut_off["due"] is True
    assert not (sessiond.state_root() / "sessiond.stopped.json").exists()


def test_threads_and_targets_show_the_pending_restart(host):
    sid = start(host, "reply: a")
    wait_for(lambda: _said(host.sup, sid) == "a")
    assert sessiond.restart_view() is None
    _pend(waiting_on=[{"session": "x", "kind": "turn", "label": "session x", "why": "w"}])
    _fresh(host.sup)
    host.sup.send(sid, "reply: later")
    view = sessiond.restart_view()
    assert view["waiting_on"] == [{"session": "x", "kind": "turn", "label": "session x"}]
    from agent_media_server import sessions

    row = {"session": sid}
    sessions.add_projects([row])
    assert row["restart_pending"]["held"] == 1 and row["restart_pending"]["state"] == "waiting"
    rs.clear_pending()
    sessions.add_projects([row])
    assert "restart_pending" not in row


# --- a session told to end its turn for the drain (23 Oct 2026) -----------------------

def _told(sid, rid="r1"):
    from agent_media_core.intake import context

    (context.state_dir() / f"{sid}.restart").write_text(rid)


def _drained(host, text="slow: 0.6"):
    """A session whose turn the pending restart waits on, told to end it (the
    context hook's marker), and then ending it: idle, but cut short."""
    sid = start(host, text)
    wait_for(lambda: state(host, sid) == "working")
    _pend(waiting_on=[{"session": sid, "kind": "turn", "label": "s", "why": "is working"}])
    _fresh(host.sup)
    _told(sid)
    wait_for(lambda: state(host, sid) == "waiting")
    return sid


def test_a_session_that_ended_its_turn_for_the_drain_is_resumed_after_the_restart(host):
    sid = _drained(host)
    cut = sessiond.read_record(sid)["cut_off"]
    assert cut["why"] == "drain" and cut["due"] and cut["turn"] and cut["restart"] == "r1"
    assert cut["last_text"] == "slow: 0.6"
    assert host.sup.deliver_due() == []            # not while the drain holds
    rs._update_pending(lambda r: {**r, "state": "firing"})
    sessiond.stop(host.srv)
    wait_for(lambda: not host.sup.sessions[sid].live)
    assert sessiond.read_record(sid)["cut_off"]["why"] == "drain"   # the stop kept it
    sup2 = sessiond.Supervisor()
    sup2.load()
    try:
        cut = sup2.sessions[sid].cut_off
        assert cut["due"] and cut["restarted"]
        note = sessiond.resume_note(sup2.sessions[sid], cut)
        assert note.startswith("[From the session host] You ended your last turn early "
                               "because a restart of the session host was waiting; it has "
                               "restarted. Carry on with what you were doing")
        assert "“slow: 0.6”" in note and "already finished" in note
        assert sup2.deliver_due() == [sid]
        wait_for(lambda: _said(sup2, sid).startswith("ok: [From the session host] You ended"))
        assert sup2.deliver_due() == []            # once
    finally:
        sup2.shutdown()


def test_a_drained_session_is_resumed_when_the_drain_gives_up_too(host):
    sid = _drained(host)
    rs.clear_pending()
    _fresh(host.sup)
    assert host.sup.deliver_due() == [sid]
    wait_for(lambda: "it is over. Carry on" in _said(host.sup, sid))
    assert sessiond.read_record(sid)["cut_off"] is None


def test_a_session_not_told_to_end_its_turn_is_not_resumed(host):
    sid = start(host, "slow: 0.6")
    wait_for(lambda: state(host, sid) == "working")
    _pend(waiting_on=[{"session": sid, "kind": "turn", "label": "s", "why": "is working"}])
    _fresh(host.sup)
    _told(sid, rid="an-older-restart")
    wait_for(lambda: state(host, sid) == "waiting")
    assert sessiond.read_record(sid)["cut_off"] is None


def test_a_drained_session_busy_again_at_the_stop_is_cut_off_mid_turn(host):
    sid = _drained(host)
    s = host.sup.sessions[sid]
    with host.sup.lock:
        s.set_state("working")                      # a task's result came in
    host.sup.begin_stop()
    cut = sessiond.read_record(sid)["cut_off"]
    assert cut["why"] == "restart" and cut["drained"] and cut["turn"]
