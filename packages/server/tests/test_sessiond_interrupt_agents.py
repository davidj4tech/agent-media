"""An interrupt ends the turn, not the session's background agents (23 Oct 2026).

Claude Code's interrupt control request, from a host that has not declared
`perTaskStopAffordance` in an `initialize`, kills every running background
agent and marks each stopped by the user; SendMessage to one is then refused
("was stopped by the user and won't be resumed"). A retracted message (the
app's Cancel) interrupts the turn, so it cost a session two agents' work. So
sessiond declares it at every spawn, and when a CLI kills them anyway the
next message says so, with how to relaunch them. Against the fake `claude`
(test_headless.py's `host`); nothing here reaches a real session.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_media_server import driver, sessiond

from test_headless import host, start, state, wait_for  # noqa: F401


def _inits(host) -> list[dict]:
    try:
        return [json.loads(ln) for ln in Path(str(host.log) + ".init").read_text().splitlines()]
    except OSError:
        return []


def _launched(host, sid, aid):
    wait_for(lambda: aid in host.sup.sessions[sid].tasks and state(host, sid) == "waiting")


def test_every_spawn_declares_per_task_stop_before_the_first_message(host):
    sid = start(host, "reply: a")
    wait_for(lambda: _inits(host))
    got = _inits(host)[0]
    assert got["request"] == {"subtype": "initialize", "perTaskStopAffordance": True}
    assert got["turns_before"] == 0
    # Its answer is not taken for anyone's receipt.
    wait_for(lambda: state(host, sid) == "waiting")
    assert not any(r.startswith("req_init_") for r in host.sup.sessions[sid].responses)
    assert not host.sup.sessions[sid].init_rids


def test_an_interrupt_spares_a_running_background_agent(host):
    sid = start(host, "agent: a1b2 Write the docs")
    _launched(host, sid, "a1b2")
    driver.headless_driver().send(sid, "", "slow: 20")
    wait_for(lambda: state(host, sid) == "working")
    ok, d = driver.headless_driver().interrupt(sid)
    assert ok and d["interrupted"] is True
    s = host.sup.sessions[sid]
    assert "a1b2" in s.tasks and s.interrupt_killed == []


def test_agents_an_interrupt_killed_anyway_are_named_on_the_next_message(host, monkeypatch):
    # A CLI that ignores the declaration: as the off switch makes it.
    monkeypatch.setenv("MEDIA_SESSIOND_SPARE_AGENTS", "0")
    sid = start(host, "agent: a1b2 Write the docs")
    _launched(host, sid, "a1b2")
    assert _inits(host) == []
    driver.headless_driver().send(sid, "", "slow: 20")
    wait_for(lambda: state(host, sid) == "working")
    ok, d = driver.headless_driver().interrupt(sid)
    assert ok and d["interrupted"] is True
    s = host.sup.sessions[sid]
    assert "a1b2" not in s.tasks
    assert s.interrupt_killed == [{"id": "a1b2", "type": "local_agent",
                                   "description": "Write the docs"}]
    assert sessiond.read_record(sid)["interrupt_killed"][0]["id"] == "a1b2"
    assert s.interrupt_watch == {}
    driver.headless_driver().send(sid, "", "reply: next")
    wait_for(lambda: state(host, sid) == "waiting" and any(
        e.get("type") == "result" and "Stopping your last turn" in str(e.get("result"))
        for e in s.events))
    said = next(str(e["result"]) for e in reversed(s.events) if e.get("type") == "result")
    assert said.startswith("ok: [From the session host] Stopping your last turn also "
                           "stopped this background agent")
    assert "“Write the docs” → a1b2" in said
    assert "launch a new agent" in said and "rather than start over" in said
    assert said.rstrip().endswith("reply: next")
    # Said once, and the thread's last message is still David's words.
    assert s.interrupt_killed == [] and s.last_text == "reply: next"
    assert sessiond.read_record(sid)["interrupt_killed"] == []


def test_a_stop_task_is_not_taken_for_an_interrupt_kill(host):
    sid = start(host, "agent: a1b2 Write the docs")
    _launched(host, sid, "a1b2")
    s = host.sup.sessions[sid]
    host.sup._on_event(s, {"type": "system", "subtype": "task_notification",
                           "task_id": "a1b2", "status": "stopped"})
    assert s.interrupt_killed == []


def test_the_initialize_answer_is_kept_small():
    big = {"type": "control_response", "response": {
        "subtype": "success", "request_id": "req_init_1a2b3c4d",
        "response": {"commands": [{"name": "x" * 50}] * 500}}}
    assert sessiond._compact(big) == {"type": "control_response", "response": {
        "subtype": "success", "request_id": "req_init_1a2b3c4d"}}
    other = {"type": "control_response", "response": {"subtype": "success",
                                                      "request_id": "req_1", "response": {"a": 1}}}
    assert sessiond._compact(other) is other
