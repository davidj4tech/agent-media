"""Resuming after the usage limit (sessiond.py, core quota.py; 20 Oct 2026).

A turn the limit ends cuts its session off (why "quota"); while the account
is limited messages are held and new chats refused; at the reset the
sessions are resumed one at a time, oldest first, each once, with a note —
and a background agent the limit stopped on its own gets its session resumed
to continue it. Against the fake `claude` (test_headless.py's `host`), whose
turns the limit refuses while `$FAKE_CLAUDE_LIMIT_FILE` exists; desk sessions
against a recorded pane seam. Nothing here reaches a real session or pane.
"""

from __future__ import annotations

import json
import time

import pytest

from agent_media_core import quota
from agent_media_server import driver, sessiond

from test_headless import _task, host, start, starts, state, wait_for  # noqa: F401

SAID = "You've hit your session limit · resets 2am (Australia/Melbourne)"


@pytest.fixture
def limit(host, monkeypatch, tmp_path):
    """Switch the fake's usage limit on (`on(reset)`) and off."""
    f = tmp_path / "limit"
    monkeypatch.setenv("FAKE_CLAUDE_LIMIT_FILE", str(f))
    monkeypatch.setenv("MEDIA_QUOTA_RESUME_MARGIN_S", "0")
    monkeypatch.setenv("MEDIA_QUOTA_RESUME_GAP_S", "30")

    class L:
        @staticmethod
        def on(reset: float) -> None:
            f.write_text(str(int(reset)))

        @staticmethod
        def off() -> None:
            f.unlink(missing_ok=True)

        @staticmethod
        def reset_now() -> None:
            """The window resets: the fake answers again, the record is due."""
            f.unlink(missing_ok=True)
            quota._update(lambda r: {**r, "resets_at": time.time() - 1} if r else r)

    return L


def _said(sup, sid) -> str:
    return next((e.get("result") for e in reversed(sup.sessions[sid].events)
                 if e.get("type") == "result"), "")


def _limited(sup, sid) -> bool:
    return ((sup.sessions[sid].cut_off or {}).get("why") == "quota"
            and state_ok(sup, sid))


def state_ok(sup, sid) -> bool:
    return sup.sessions[sid].state == "waiting"


def test_a_limited_turn_is_cut_off_held_and_resumed_once_at_the_reset(host, limit):
    sid = start(host, "reply: a")
    wait_for(lambda: _said(host.sup, sid) == "a")
    reset = time.time() + 3600
    limit.on(reset)
    host.sup.send(sid, "reply: b")
    wait_for(lambda: _limited(host.sup, sid))
    cut = sessiond.read_record(sid)["cut_off"]
    assert cut["why"] == "quota" and cut["turn"] and cut["resets_at"] == int(reset)
    assert cut["last_text"] == "reply: b"
    rec = quota.read()
    assert rec["resets_at"] == int(reset) and [w["session"] for w in rec["waiting"]] == [sid]
    # David writes meanwhile: held, not a turn that fails.
    ok, d = driver.headless_driver().send(sid, "", "reply: c")
    assert ok and d["held"] is True and d["quota"]["resets_at"] == int(reset)
    assert d["quota"]["state"] == "waiting"
    n = len([e for e in host.sup.sessions[sid].events if e.get("type") == "result"])
    # A new chat is refused, saying when.
    ok, d = driver.headless_driver().start(agent="claude", cwd=str(host.work), text="hi")
    assert not ok and d["code"] == "quota_limited" and d["status"] == 503
    assert "resets at" in d["error"] and d["quota"]["resets_at"] == int(reset)
    # Not before the reset.
    assert host.sup.deliver_due() == []
    limit.reset_now()
    assert host.sup.deliver_due() == [sid]
    wait_for(lambda: _said(host.sup, sid) == "c")
    notes = [e for e in host.sup.sessions[sid].events if e.get("type") == "result"][n:]
    first = notes[0]["result"]
    assert first.startswith("ok: [From the session host] You were stopped by the usage "
                            "limit at ")
    assert "it has reset. Pick up where you left off" in first and "reply: b" in first
    assert sessiond.read_record(sid)["cut_off"] is None
    # Never twice; the record is gone once nobody waits.
    k = len(starts(host))
    assert host.sup.deliver_due() == [] and len(starts(host)) == k
    assert quota.read() is None
    assert sessiond.read_record(sid)["quota_resumed"] == cut["at"]


def test_resumes_are_staggered_and_one_that_hits_it_again_stops_the_rest(host, limit):
    a = start(host, "reply: a")
    b = start(host, "reply: b")
    wait_for(lambda: _said(host.sup, a) == "a" and _said(host.sup, b) == "b")
    limit.on(time.time() + 3600)
    host.sup.send(a, "reply: a2")
    wait_for(lambda: _limited(host.sup, a))
    # b was mid-turn already (a message now would be held): it hits it too.
    host.sup.send(b, "reply: b2", _internal=True)
    wait_for(lambda: _limited(host.sup, b))
    assert [w["session"] for w in quota.read()["waiting"]] == [a, b]
    # The reset passes but the limit is still on: the first resumed (the
    # oldest) is refused again, the reset moves, and b is not tried.
    quota._update(lambda r: {**r, "resets_at": time.time() - 1})
    later = time.time() + 7200
    limit.on(later)
    assert host.sup.deliver_due() == [a]
    wait_for(lambda: quota.read()["state"] == "limited")
    rec = quota.read()
    assert rec["resets_at"] == int(later)
    assert [w["session"] for w in rec["waiting"]] == [b, a]
    assert sessiond.read_record(a)["cut_off"]["why"] == "quota"
    b_results = len([e for e in host.sup.sessions[b].events if e.get("type") == "result"])
    assert host.sup.deliver_due() == []
    # Now it resets: b (oldest now) first, then a only after the gap.
    limit.reset_now()
    assert host.sup.deliver_due() == [b]
    assert host.sup.deliver_due() == []                       # the gap
    quota._update(lambda r: {**r, "next_at": time.time() - 1})
    assert host.sup.deliver_due() == [a]
    wait_for(lambda: "Pick up where you left off" in _said(host.sup, a))
    wait_for(lambda: len([e for e in host.sup.sessions[b].events
                          if e.get("type") == "result"]) == b_results + 1)


def test_the_wait_survives_a_restart_of_the_host(host, limit):
    sid = start(host, "reply: a")
    wait_for(lambda: _said(host.sup, sid) == "a")
    limit.on(time.time() + 3600)
    host.sup.send(sid, "reply: b")
    wait_for(lambda: _limited(host.sup, sid))
    sessiond.stop(host.srv)
    wait_for(lambda: not host.sup.sessions[sid].live)
    sup2 = sessiond.Supervisor()
    sup2.load()
    try:
        cut = sup2.sessions[sid].cut_off
        assert cut["why"] == "quota" and not cut.get("due")
        assert sup2.deliver_due() == []
        limit.reset_now()
        assert sup2.deliver_due() == [sid]
        wait_for(lambda: "usage limit" in _said(sup2, sid))
        assert "--resume" in starts(host)[-1]["argv"]
    finally:
        sup2.shutdown()


def test_an_agent_the_limit_stopped_on_its_own_gets_its_session_resumed(host, limit):
    sid = start(host, "reply: a")
    wait_for(lambda: _said(host.sup, sid) == "a")
    _task(host, sid, "task_started", description="Write the docs", task_type="local_agent",
          is_backgrounded=True)
    _task(host, sid, "task_updated", patch={
        "status": "failed", "error": "Agent terminated early due to an API error: " + SAID})
    _task(host, sid, "task_notification", status="failed", summary="Agent terminated early "
          "due to an API error: " + SAID)
    cut = sessiond.read_record(sid)["cut_off"]
    assert cut["why"] == "quota" and cut["turn"] is False
    assert cut["tasks"] == [{"id": "a3f6", "description": "Write the docs",
                             "type": "local_agent"}]
    rec = quota.read()
    assert [w["session"] for w in rec["waiting"]] == [sid]
    assert rec["resets_at"] > time.time()            # from the sentence: 2am next
    note = sessiond.quota_note(cut)
    assert note.startswith("[From the session host] A background agent of yours was "
                           "stopped by the usage limit at ")
    assert "SendMessage to its id" in note and "don't start a new one" in note
    assert "“Write the docs” → a3f6" in note and "tell David" in note
    limit.reset_now()
    assert host.sup.deliver_due() == [sid]
    wait_for(lambda: "“Write the docs” → a3f6" in _said(host.sup, sid))


def test_a_command_that_finished_while_limited_is_named_in_the_note(host, limit):
    sid = start(host, "reply: a")
    wait_for(lambda: _said(host.sup, sid) == "a")
    _task(host, sid, "task_started", description="npm test", task_type="local_bash",
          is_backgrounded=True, task_id="b1")
    limit.on(time.time() + 3600)
    host.sup.send(sid, "reply: b")
    wait_for(lambda: _limited(host.sup, sid))
    _task(host, sid, "task_notification", task_id="b1", status="completed",
          output_file="/tmp/b1.output")
    cut = sessiond.read_record(sid)["cut_off"]
    assert cut["finished"][0]["description"] == "npm test"
    note = sessiond.quota_note(cut)
    assert "npm test (output: /tmp/b1.output)" in note and "can't be resumed" not in note


def test_a_message_after_the_reset_resumes_its_session_out_of_turn(host, limit):
    sid = start(host, "reply: a")
    wait_for(lambda: _said(host.sup, sid) == "a")
    limit.on(time.time() + 3600)
    host.sup.send(sid, "reply: b")
    wait_for(lambda: _limited(host.sup, sid))
    limit.reset_now()
    r = host.sup.send(sid, "reply: now")
    assert not r.get("held")
    assert quota.waiting_entry(sid) is None
    wait_for(lambda: _said(host.sup, sid) == "now")
    assert any("usage limit" in str(e.get("result")) for e in host.sup.sessions[sid].events)
    assert sessiond.read_record(sid)["cut_off"] is None


def _events(root, sid, evs):
    with open(root / f"{sid}.events.jsonl", "w") as fh:
        for i, ev in enumerate(evs, 1):
            fh.write(json.dumps({"seq": i, "t": 1792418000.0 + i, **ev}) + "\n")


def test_a_session_limited_before_this_was_running_is_found_at_load(host, limit):
    root = sessiond.state_root()
    root.mkdir(parents=True, exist_ok=True)
    sid = "6a1e0000-0000-4000-8000-0000000000aa"
    done = "6a1e0000-0000-4000-8000-0000000000bb"
    reset = 1792422000
    limited = [
        {"type": "system", "subtype": "task_started", "task_id": "a69", "task_type": "local_agent",
         "description": "Tap a picture to enlarge"},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": reset,
                                                         "rateLimitType": "five_hour"}},
        {"type": "system", "subtype": "task_updated", "task_id": "a69",
         "patch": {"status": "failed", "error": "Agent terminated early: " + SAID}},
        {"type": "result", "is_error": True, "terminal_reason": "api_error",
         "api_error_status": 429, "api_error": "usage_limit_reached", "result": SAID}]
    _events(root, sid, limited)
    # One resumed since: its last result is not the limit's.
    _events(root, done, limited + [{"type": "result", "is_error": False, "result": "ok"}])
    now = time.time()
    for s, err in ((sid, True), (done, False)):
        (root / f"{s}.json").write_text(json.dumps({
            "session": s, "cwd": str(host.work), "state": "parked", "last_event_at": now,
            "last_text": "make it bigger",
            "last_result": {"is_error": err, "terminal_reason": "api_error" if err else
                            "completed"}}))
    sup = sessiond.Supervisor()
    sup.load()
    cut = sup.sessions[sid].cut_off
    assert cut["why"] == "quota" and cut["backfilled"] and cut["resets_at"] == reset
    assert cut["tasks"] == [{"id": "a69", "type": "local_agent",
                             "description": "Tap a picture to enlarge"}]
    assert sup.sessions[done].cut_off is None
    assert [w["session"] for w in quota.read()["waiting"]] == [sid]
    # Its reset is long past: it goes at once.
    assert sup.deliver_due() == [sid]
    wait_for(lambda: "“Tap a picture to enlarge” → a69" in _said(sup, sid))
    assert "make it bigger" in _said(sup, sid)
    sup.shutdown()
    # Loaded again: it is not found twice.
    sup3 = sessiond.Supervisor()
    sup3.load()
    assert sup3.sessions[sid].cut_off is None and quota.waiting_entry(sid) is None
    sup3.deliver_due()
    assert quota.read() is None                     # nobody left waiting: gone


def test_rows_and_targets_carry_the_wait(host, limit):
    sid = start(host, "reply: a")
    wait_for(lambda: _said(host.sup, sid) == "a")
    from agent_media_server import sessions

    row = {"session": sid}
    sessions.add_projects([row])
    assert "quota" not in row and sessiond.quota_view() is None
    reset = time.time() + 3600
    limit.on(reset)
    host.sup.send(sid, "reply: b")
    wait_for(lambda: _limited(host.sup, sid))
    sessions.add_projects([row])
    assert row["quota"] == {"resets_at": int(reset), "state": "waiting"}
    assert sessiond.quota_view()["waiting"] == 1
    other = {"session": "someone-else"}
    sessions.add_projects([other])
    assert "quota" not in other


# --- desk sessions ---------------------------------------------------------------

@pytest.fixture
def pane(monkeypatch, tmp_path):
    """A desk session in pane %5, its transcript ending on the limit; the
    seam that would type into it records instead."""
    from agent_media_server import panes, send, sessions, transcript

    tp = tmp_path / "desk.jsonl"
    tp.write_text(json.dumps({"type": "assistant", "isApiErrorMessage": True,
                              "apiError": "usage_limit_reached",
                              "message": {"content": [{"type": "text", "text": SAID}]}}) + "\n")
    ns = type("Pane", (), {"typed": [], "draft": True, "tp": tp})()
    monkeypatch.setattr(sessions, "live_sessions", lambda: {"desk-1": "%5"})
    monkeypatch.setattr(panes, "alive", lambda p: p == "%5")
    monkeypatch.setattr(panes, "classify", lambda p, a="claude": "idle")
    monkeypatch.setattr(sessions, "pane_draft", lambda p: ns.draft)
    monkeypatch.setattr(sessions, "agent_of", lambda s: "claude")
    monkeypatch.setattr(transcript, "transcript_path", lambda s: str(tp))
    monkeypatch.setattr(send, "_send_to_pane", lambda p, t: ns.typed.append((p, t)) or "")
    monkeypatch.setattr(send, "_ensure_submitted", lambda *a, **k: True)
    monkeypatch.setenv("MEDIA_QUOTA_RESUME_MARGIN_S", "0")
    return ns


def test_a_desk_session_gets_the_note_in_its_pane_never_over_a_draft(host, pane):
    quota.note_limit({"kind": "desk", "session": "desk-1", "cwd": "/w", "pane": "%5",
                      "turn": True, "last_text": "fix the build",
                      "tasks": [{"id": "a1", "description": "Write the docs",
                                 "type": "local_agent"}]}, time.time() - 1)
    assert host.sup.quota_tick() == ""                 # a draft: back in line
    assert pane.typed == [] and quota.waiting_entry("desk-1")["tries"] == 1
    pane.draft = False
    quota._update(lambda r: {**r, "next_at": time.time() - 1})
    assert host.sup.quota_tick() == "desk-1"
    ((where, text),) = pane.typed
    assert where == "%5"
    assert "You were stopped by the usage limit" in text and "fix the build" in text
    assert "“Write the docs” → a1" in text and "SendMessage" in text
    assert quota.read() is None or quota.waiting_entry("desk-1") is None


def test_a_desk_session_picked_up_already_is_left_alone(host, pane):
    pane.draft = False
    pane.tp.write_text(pane.tp.read_text() + json.dumps(
        {"type": "user", "message": {"content": "carry on"}}) + "\n")
    quota.note_limit({"kind": "desk", "session": "desk-1", "turn": True}, time.time() - 1)
    assert host.sup.quota_tick() == ""
    assert pane.typed == [] and quota.waiting_entry("desk-1") is None


def test_antigravity_and_rate_limit_warning_notifications(host, monkeypatch):
    from agent_media_server import alerts, antigravity_adapter, notes

    posted = []
    monkeypatch.setattr(notes, "post", lambda title, body="", **kw: posted.append((title, body, kw)) or True)
    s = sessiond.Session("6a1e0000-0000-4000-8000-0000000000cc", str(host.work), agent="antigravity", workspace="amux")
    host.sup.sessions[s.id] = s
    # Rate limit warning event triggers alert + phone notification
    reset = time.time() + 1800
    host.sup._on_event(s, {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "allowed_warning",
            "utilization": 0.85,
            "rateLimitType": "five_hour",
            "resetsAt": reset,
        },
    })
    wait_for(lambda: any("quota at 85%" in t for t, _, _ in posted))
    open_alerts = [a for a in alerts.listing(open_only=True)["alerts"] if a["id"] == "quota.antigravity"]
    assert len(open_alerts) == 1 and open_alerts[0]["step"] == 80

    # Antigravity streamed reply is preserved for speech even when result.response is empty
    spoken = []
    monkeypatch.setattr(antigravity_adapter, "speak", lambda sess, txt: spoken.append((sess.id, txt)))
    s.agy_queue.append("u1")
    antigravity_adapter.event(host.sup, s, {
        "event": "step_update",
        "step_update": {"step_type": "agent_response", "step_index": 1, "text_delta": "Hello from streamed step!", "state": "DONE"},
    })
    antigravity_adapter.event(host.sup, s, {
        "event": "result",
        "result": {"status": "DONE", "response": "", "duration_seconds": 0.5},
    })
    wait_for(lambda: spoken == [("6a1e0000-0000-4000-8000-0000000000cc", "Hello from streamed step!")])

    # Antigravity hitting quota limit records cut_off and emits limit notification
    s.last_text = "keep coding"
    antigravity_adapter.event(host.sup, s, {
        "event": "result",
        "result": {
            "status": "ERROR",
            "error": "RESOURCE_EXHAUSTED: You have exhausted your capacity. Try again in 30 minutes.",
        },
    })
    assert s.cut_off is not None and s.cut_off["why"] == "quota"
    assert quota.limited(agent="antigravity") is not None
    # Claude is NOT blocked by Antigravity's limit
    assert quota.limited(agent="claude") is None
