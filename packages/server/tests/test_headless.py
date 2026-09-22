"""Headless sessions: media-sessiond (sessiond.py) and the headless driver
(driver/headless.py), end to end against a fake `claude`.

sessiond runs in-process on a throwaway socket; the agent it spawns is
`fixtures/fake_claude.py`, which speaks the envelopes the spike recorded
(docs/notes/2026-09-22-headless-spike.md) and writes a small transcript under
the conftest's throwaway CLAUDE_CONFIG_DIR. Nothing here starts a real
`claude`, types into a pane or reaches a live server.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from agent_media_server import driver, permissions, sessiond
from agent_media_server.driver import headless as hd

FAKE = Path(__file__).parent / "fixtures" / "fake_claude.py"


def wait_for(pred, timeout: float = 8.0, step: float = 0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = pred()
        if got:
            return got
        time.sleep(step)
    raise AssertionError("timed out waiting")


@pytest.fixture()
def host(monkeypatch, tmp_path):
    """A running sessiond with the fake claude, and the flag on."""
    sockdir = tempfile.mkdtemp(prefix="sd", dir="/tmp")     # AF_UNIX paths are short
    sock = Path(sockdir) / "s.sock"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("MEDIA_HEADLESS", "1")
    monkeypatch.setenv("MEDIA_SESSIOND_SOCKET", str(sock))
    monkeypatch.setenv("MEDIA_SESSIOND_CLAUDE", str(FAKE))
    monkeypatch.setenv("MEDIA_SESSIOND_CLOSE_GRACE", "3")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "fake.log"))
    monkeypatch.setenv("FAKE_CLAUDE_TICK", "0.01")
    # What a pane's environment would leak into a child, to prove it does not.
    monkeypatch.setenv("TMUX_PANE", "%99")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-pass")
    monkeypatch.setenv("CLAUDECODE", "1")
    # The listener's turn is recorded in the background (a render); not here.
    from agent_media_server import send

    turns: list = []
    monkeypatch.setattr(send, "_record_turn", lambda s, t, p="": turns.append((s, t, p)))
    driver._reset_for_tests()
    sup = sessiond.Supervisor()
    srv, _t = sessiond.serve(sock, sup, tick=3600)
    ns = type("Host", (), {})()
    ns.sup, ns.srv, ns.work, ns.log, ns.turns = sup, srv, work, tmp_path / "fake.log", turns
    ns.sock = sock
    yield ns
    try:
        sessiond.stop(srv)
    except Exception:  # noqa: BLE001 — a test may have stopped it already
        pass
    for s in sup.sessions.values():
        if s.proc is not None and s.proc.poll() is None:
            s.proc.kill()
    shutil.rmtree(sockdir, ignore_errors=True)
    driver._reset_for_tests()


def starts(h) -> list[dict]:
    try:
        return [json.loads(ln) for ln in h.log.read_text().splitlines()]
    except OSError:
        return []


def state(h, sid) -> str:
    return h.sup.get(sid)["state"]


def last_text(h, sid) -> str:
    return (h.sup.sessions[sid].last_result or {}) and next(
        (e.get("result") for e in reversed(h.sup.sessions[sid].events)
         if e.get("type") == "result"), "")


def start(h, text, **kw):
    ok, d = driver.headless_driver().start(agent="claude", cwd=str(h.work), text=text,
                                          host=kw.pop("host", "p-work"), **kw)
    assert ok, d
    return d["session"]


# --- starting, sending, the process -------------------------------------------------

def test_start_spawns_claude_headless_with_the_spike_flags(host):
    sid = start(host, "reply: hello")
    wait_for(lambda: last_text(host, sid) == "hello")
    assert state(host, sid) == "waiting"
    run = starts(host)[0]
    argv = run["argv"]
    for flag in ("-p", "--verbose", "--input-format", "--output-format"):
        assert flag in argv
    assert argv[argv.index("--permission-prompt-tool") + 1] == "stdio"
    assert argv[argv.index("--session-id") + 1] == sid
    assert "--resume" not in argv
    assert run["cwd"] == str(host.work)
    env = run["env"]
    assert env["MEDIA_SOURCE_KIND"] == "headless"
    assert env["MEDIA_SOURCE_WORKSPACE"] == "p-work"
    assert env["MEDIA_SESSIOND_SESSION"] == sid
    assert not any(k.startswith(("TMUX", "ANTHROPIC")) for k in env)
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CONFIG_DIR" in env              # configuration, kept
    # The listener's own words are shelved, with no pane.
    assert host.turns == [(sid, "reply: hello", "")]
    # The record is on disk, where the canvas reads which sessions are headless.
    rec = sessiond.read_record(sid)
    assert rec["driver"] == "headless" and rec["cwd"] == str(host.work)
    assert driver.owned_headless(sid) and driver.for_session(sid).kind == "headless"


def test_the_transcript_lands_where_claude_puts_it(host):
    from agent_media_server import sessions, transcript

    sid = start(host, "reply: pineapple")
    wait_for(lambda: last_text(host, sid) == "pineapple")
    assert sessions.session_exists(sid)
    assert sessiond.read_record(sid)["cwd"] == str(host.work)
    msgs, _older = transcript.messages(sid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["parts"][-1]["text"] == "pineapple"


def test_send_keeps_newlines_and_puts_a_quote_in_its_own_paragraph(host):
    assert hd.compose("line one\nline two", "earlier\nwords") == \
        "> earlier\n> words\n\nline one\nline two"
    sid = start(host, "reply: a")
    wait_for(lambda: state(host, sid) == "waiting")
    ok, d = driver.headless_driver().send(sid, "flattened body", "reply: b\nsecond line")
    assert ok and d["submitted"] and d["driver"] == "headless" and d["pane"] is None
    wait_for(lambda: "b\nsecond line" in (last_text(host, sid) or ""))


def test_a_message_mid_turn_is_queued_and_joins_the_turn(host):
    sid = start(host, "slow: 1.2")
    wait_for(lambda: any(e.get("subtype") == "task_started" for e in host.sup.sessions[sid].events))
    ok, d = driver.headless_driver().send(sid, "", "reply: banana")
    assert ok and d["queued"] is True and d["acked"] is True
    wait_for(lambda: last_text(host, sid) == "slept\nbanana", timeout=10)
    assert host.sup.sessions[sid].last_result["num_turns"] == 2
    assert state(host, sid) == "waiting"


def test_state_comes_from_events_not_sends(host):
    sid = start(host, "slow: 0.6")
    assert driver.headless_driver().state(sid)["state"] == "working"
    wait_for(lambda: state(host, sid) == "waiting", timeout=10)
    assert driver.headless_driver().state(sid) == {"state": "waiting", "live": True,
                                                  "pane": None}


# --- interrupt ----------------------------------------------------------------------

def test_interrupt_mid_tool_has_a_receipt_and_the_next_message_runs(host):
    sid = start(host, "slow: 20")
    wait_for(lambda: any(e.get("subtype") == "task_started" for e in host.sup.sessions[sid].events))
    queued = driver.headless_driver().send(sid, "", "reply: plum")[1]
    ok, d = driver.headless_driver().interrupt(sid)
    assert ok and d["interrupted"] is True and d["why"] is None
    # The receipt names the queued message by the uuid it was sent with.
    assert d["receipt"] == {"still_queued": [queued["uuid"]]}
    # The queued message then runs as its own turn (no cancel_queued).
    wait_for(lambda: last_text(host, sid) == "plum")
    ok, d = driver.headless_driver().send(sid, "", "reply: mango")
    assert ok
    wait_for(lambda: last_text(host, sid) == "mango")


def test_interrupt_when_idle_or_on_an_approval_does_nothing(host):
    sid = start(host, "reply: x")
    wait_for(lambda: state(host, sid) == "waiting")
    ok, d = driver.headless_driver().interrupt(sid)
    assert ok and d["interrupted"] is False and d["why"] == "not working"
    sid2 = start(host, "tool: touch a")
    wait_for(lambda: state(host, sid2) == "approval")
    ok, d = driver.headless_driver().interrupt(sid2)
    assert ok and d["interrupted"] is False and d["why"] == "waiting on a question"
    assert state(host, sid2) == "approval"


# --- approvals ----------------------------------------------------------------------

def test_a_tool_request_is_a_structured_approval_and_allow_runs_it(host):
    sid = start(host, "tool: touch allowed.txt")
    wait_for(lambda: state(host, sid) == "approval")
    appr = driver.headless_driver().approval(sid)
    assert appr["kind"] == "tool" and appr["tool"] == "Bash"
    assert appr["input_summary"] == "touch allowed.txt"
    assert appr["input"]["command"] == "touch allowed.txt"
    assert appr["id"] and appr["tool_use_id"].startswith("toolu_")
    assert appr["suggestions"][0]["destination"] == "localSettings"
    # The v0 fields, so a numbered client can answer it too.
    assert [o["label"] for o in appr["options"]] == ["Allow", "Deny"]
    assert len(appr["key"]) == 12 and appr["agent"] == "claude"
    assert appr["question"] == "Allow Bash: touch allowed.txt?"
    ok, d = driver.headless_driver().answer(sid, {"request_id": appr["id"], "decision": "allow"})
    assert ok and d["decision"] == "allow" and d["waiting"] is False and d["approval"] is None
    wait_for(lambda: last_text(host, sid) == "ran touch allowed.txt")
    assert state(host, sid) == "waiting"


def test_deny_reaches_the_agent_with_its_message(host):
    sid = start(host, "tool: rm -rf x")
    wait_for(lambda: state(host, sid) == "approval")
    appr = driver.headless_driver().approval(sid)
    ok, d = driver.headless_driver().answer(
        sid, {"request_id": appr["id"], "decision": "deny", "message": "not today"})
    assert ok and d["decision"] == "deny"
    wait_for(lambda: last_text(host, sid) == "not allowed to run rm -rf x")
    sent = [e for e in host.sup.sessions[sid].events if e.get("type") == "user"]
    assert any("not today" in json.dumps(e) for e in sent)


def test_numbered_answer_still_works_on_a_headless_session(host):
    sid = start(host, "tool: ls")
    wait_for(lambda: state(host, sid) == "approval")
    appr = driver.headless_driver().approval(sid)
    ok, d = driver.headless_driver().answer(sid, {"choice": 9, "key": appr["key"]})
    assert not ok and d["status"] == 400
    ok, d = driver.headless_driver().answer(sid, {"choice": 1, "key": "stale0000000"})
    assert not ok and d["status"] == 409 and d["approval"]["id"] == appr["id"]
    ok, d = driver.headless_driver().answer(sid, {"choice": 1, "key": appr["key"]})
    assert ok and d["answered"] == 1 and d["label"] == "Allow"
    wait_for(lambda: last_text(host, sid) == "ran ls")


def test_a_stale_request_id_is_the_question_has_changed(host):
    sid = start(host, "tool: ls")
    wait_for(lambda: state(host, sid) == "approval")
    ok, d = driver.headless_driver().answer(sid, {"request_id": "nope", "decision": "allow"})
    assert not ok and d["status"] == 409 and d["error"] == "the question has changed"
    ok, d = driver.headless_driver().answer(sid, {"request_id": d["approval"]["id"],
                                                  "decision": "maybe"})
    assert not ok and d["status"] == 400


def test_a_question_is_answered_with_multi_select_and_free_text(host):
    sid = start(host, "ask")
    wait_for(lambda: state(host, sid) == "approval")
    appr = driver.headless_driver().approval(sid)
    assert appr["kind"] == "question" and appr["tool"] == "AskUserQuestion"
    qs = appr["questions"]
    assert [q["question"] for q in qs] == ["Which fruits do you like?", "What should I call you?"]
    assert qs[0]["multiSelect"] is True and [o["label"] for o in qs[0]["options"]] == \
        ["apple", "pear", "fig"]
    # Two questions: a number cannot answer it.
    assert appr["options"] == [] and appr["partial"] is True
    ok, d = driver.headless_driver().answer(sid, {"request_id": appr["id"], "decision": "allow",
                                                  "answers": {"Which fruits do you like?": "x"}})
    assert not ok and d["status"] == 400 and "What should I call you?" in d["error"]
    ok, d = driver.headless_driver().answer(sid, {
        "request_id": appr["id"],
        "answers": {"Which fruits do you like?": ["apple", "pear"],
                    "What should I call you?": "Something else entirely"}})
    assert ok and d["decision"] == "allow"
    assert d["answers"] == {"Which fruits do you like?": "apple, pear",
                            "What should I call you?": "Something else entirely"}
    wait_for(lambda: last_text(host, sid) == "answers: apple, pear; Something else entirely")


def test_a_question_takes_the_answer_shape_a_pane_takes(host):
    # The same `[{question_index, selected, other_text}]` and `key` a pane's
    # question is answered with (asks.py), so the phone has one card.
    sid = start(host, "ask")
    wait_for(lambda: state(host, sid) == "approval")
    appr = driver.headless_driver().approval(sid)
    assert appr["multiSelect"] is True and appr["free_text"] is True
    pear = appr["questions"][0]["options"][1]
    assert (pear["n"], pear["label"], pear["checked"]) == (2, "pear", False)
    ok, d = driver.headless_driver().answer(sid, {"key": appr["key"], "answers": [
        {"question_index": 0, "selected": [9]}, {"question_index": 1, "other_text": "Sam"}]})
    assert not ok and d["status"] == 400 and "no option 9" in d["error"]
    ok, d = driver.headless_driver().answer(sid, {"key": "stale0000000", "answers": []})
    assert not ok and d["status"] == 409
    ok, d = driver.headless_driver().answer(sid, {"key": appr["key"], "answers": [
        {"question_index": 0, "selected": [1, 3]},
        {"question_index": 1, "selected": [], "other_text": "Sam"}]})
    assert ok and d["decision"] == "allow"
    assert d["answers"] == {"Which fruits do you like?": "apple, fig",
                            "What should I call you?": "Sam"}
    wait_for(lambda: last_text(host, sid) == "answers: apple, fig; Sam")


# --- parking and resuming -----------------------------------------------------------

def test_an_idle_session_is_parked_and_resumed_on_the_next_message(host, monkeypatch):
    sid = start(host, "reply: pineapple")
    wait_for(lambda: state(host, sid) == "waiting")
    pid = host.sup.get(sid)["pid"]
    monkeypatch.setenv("MEDIA_SESSIOND_IDLE", "0")
    assert host.sup.park_idle(time.time() + 5) == [sid]
    wait_for(lambda: state(host, sid) == "parked")
    assert not host.sup.get(sid)["live"]
    assert driver.headless_driver().state(sid)["state"] == "ended"
    ok, d = driver.headless_driver().send(sid, "", "reply: again")
    assert ok and d["opened"] is True
    wait_for(lambda: last_text(host, sid) == "again")
    second = starts(host)[-1]
    assert second["argv"][second["argv"].index("--resume") + 1] == sid
    assert "--session-id" not in second["argv"]
    assert host.sup.get(sid)["pid"] != pid


def test_never_parks_a_session_waiting_on_an_approval(host, monkeypatch):
    sid = start(host, "tool: touch z")
    wait_for(lambda: state(host, sid) == "approval")
    monkeypatch.setenv("MEDIA_SESSIOND_IDLE", "0")
    assert host.sup.park_idle(time.time() + 5) == []
    assert host.sup.get(sid)["live"]


def test_a_full_host_parks_the_least_recent_idle_session_or_refuses(host, monkeypatch):
    monkeypatch.setenv("MEDIA_SESSIOND_MAX", "1")
    a = start(host, "reply: a")
    wait_for(lambda: state(host, a) == "waiting")
    b = start(host, "slow: 5")
    wait_for(lambda: state(host, a) == "parked")
    wait_for(lambda: state(host, b) == "working")
    ok, d = driver.headless_driver().start(agent="claude", cwd=str(host.work), text="reply: c")
    assert not ok and d["status"] == 503 and d["code"] == "busy"
    assert "busy" in d["error"]


def test_resume_brings_a_parked_session_back_saying_nothing(host, monkeypatch):
    sid = start(host, "reply: a")
    wait_for(lambda: state(host, sid) == "waiting")
    monkeypatch.setenv("MEDIA_SESSIOND_IDLE", "0")
    host.sup.park_idle(time.time() + 5)
    wait_for(lambda: state(host, sid) == "parked")
    ok, d = driver.headless_driver().resume(sid)
    assert ok and d["opened"] is True and d["live"] is True and d["pane"] is None
    ok, d = driver.headless_driver().resume(sid)
    assert ok and d["opened"] is False


def test_a_crashed_agent_is_ended_and_the_next_message_resumes_it(host):
    sid = start(host, "reply: a")
    wait_for(lambda: state(host, sid) == "waiting")
    driver.headless_driver().send(sid, "", "crash")
    wait_for(lambda: state(host, sid) == "ended")
    assert host.sup.get(sid)["exit_code"] == 3
    ok, d = driver.headless_driver().send(sid, "", "reply: back")
    assert ok and d["opened"] is True
    wait_for(lambda: last_text(host, sid) == "back")


# --- close ---------------------------------------------------------------------------

def test_close_ends_the_process_and_a_reply_resumes_it(host):
    sid = start(host, "reply: a")
    wait_for(lambda: state(host, sid) == "waiting")
    ok, d = driver.headless_driver().close(sid)
    assert ok and d == {"session": sid, "pane": None, "live": False, "closed": True,
                        "driver": "headless"}
    wait_for(lambda: state(host, sid) == "closed")
    ok, d = driver.headless_driver().close(sid)
    assert ok and d["closed"] is False
    # Still headless: a reply goes back to sessiond, never to a pane.
    assert driver.for_session(sid).kind == "headless"
    ok, d = driver.headless_driver().send(sid, "", "reply: reopened")
    assert ok and d["opened"] is True
    wait_for(lambda: last_text(host, sid) == "reopened")


# --- sessiond restarting ---------------------------------------------------------------

def test_a_restart_with_a_pending_approval_loses_it_explicitly(host):
    sid = start(host, "tool: touch q")
    wait_for(lambda: state(host, sid) == "approval")
    appr = driver.headless_driver().approval(sid)
    sessiond.stop(host.srv)                        # what systemd's stop does
    wait_for(lambda: not host.sup.sessions[sid].live)
    # A fresh instance takes over the records.
    sup2 = sessiond.Supervisor()
    sup2.load()
    srv2, _ = sessiond.serve(host.sock, sup2, tick=3600)
    try:
        rec = sup2.get(sid)
        assert rec["state"] in ("parked", "ended") and rec["pending"] == []
        assert rec["lost"][0]["request_id"] == appr["id"]
        # Not live, so nothing to answer; the thread shows no approval.
        assert driver.headless_driver().approval(sid) is None
        ok, d = driver.headless_driver().answer(sid, {"request_id": appr["id"], "decision": "allow"})
        assert not ok and d["status"] == 404
        # The next message resumes it; answering the old request is refused
        # with the reason.
        driver.headless_driver().send(sid, "", "reply: carry on")
        wait_for(lambda: sup2.get(sid)["state"] == "waiting")
        ok, d = driver.headless_driver().answer(sid, {"request_id": appr["id"], "decision": "allow"})
        assert not ok and d["status"] == 409 and d["code"] == "lost"
        assert "send a message to carry on" in d["error"]
    finally:
        sessiond.stop(srv2)


def test_a_crashed_instance_leaves_pending_requests_lost_and_orphans_are_ended(host, tmp_path):
    sid = "5751a7c1-bd97-4c75-888c-440aad61bfd2"
    root = sessiond.state_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{sid}.json").write_text(json.dumps({
        "session": sid, "cwd": str(host.work), "state": "approval",
        "pending": [{"request_id": "r1", "request": {"subtype": "can_use_tool",
                                                     "tool_name": "Bash", "input": {}}}]}))
    # An agent a crashed instance left running for that session.
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                              env={**os.environ, "MEDIA_SESSIOND_SESSION": sid})
    try:
        sup = sessiond.Supervisor()
        sup.load()
        rec = sup.get(sid)
        assert rec["state"] == "ended" and rec["pending"] == []
        assert rec["lost"][0]["request_id"] == "r1"
        assert rec["lost"][0]["why"] == "the session host restarted"
        assert orphan.wait(5) == -signal.SIGTERM
        with pytest.raises(sessiond.Refused) as e:
            sup.answer(sid, "r1", {"behavior": "allow"})
        assert e.value.code == "lost"
    finally:
        if orphan.poll() is None:
            orphan.kill()


def test_sessiond_down_is_503_and_never_a_pane(host):
    sid = start(host, "reply: a")
    wait_for(lambda: state(host, sid) == "waiting")
    sessiond.stop(host.srv)
    assert driver.for_session(sid).kind == "headless"     # from the record on disk
    ok, d = driver.headless_driver().send(sid, "", "reply: b")
    assert not ok and d["status"] == 503 and d["code"] == "down"
    assert driver.headless_driver().state(sid)["live"] is False


# --- permissions -------------------------------------------------------------------------

def test_strict_is_the_default_and_mirrors_the_allow_rules_as_asks(host, tmp_path, monkeypatch):
    cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "settings.json").write_text(json.dumps({"permissions": {
        "defaultMode": "auto",
        "allow": ["Bash(*)", "Write(*)", "mcp__venice", "Read(//home/**)"]}}))
    (host.work / ".claude").mkdir()
    (host.work / ".claude" / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(git *)"]}}))
    sid = start(host, "reply: a")
    wait_for(lambda: state(host, sid) == "waiting")
    argv = starts(host)[0]["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "default"
    overlay = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
    perms = overlay["permissions"]
    assert perms["defaultMode"] == "default"
    assert "Read" in perms["allow"] and "Bash" not in perms["allow"]
    for rule in ("Bash", "Write", "Edit", "WebFetch", "Bash(*)", "Write(*)", "mcp__venice",
                 "Bash(git *)"):
        assert rule in perms["ask"], rule
    assert "Read(//home/**)" not in perms["ask"]          # a safe tool stays allowed
    assert sessiond.read_record(sid)["permissions"] == "strict"


def test_normal_permissions_load_the_users_own_settings(host, monkeypatch):
    monkeypatch.setenv("MEDIA_HEADLESS_PERMISSIONS", "normal")
    sid = start(host, "reply: a")
    wait_for(lambda: state(host, sid) == "waiting")
    argv = starts(host)[0]["argv"]
    assert "--settings" not in argv and "--permission-mode" not in argv
    assert argv[argv.index("--permission-prompt-tool") + 1] == "stdio"
    assert sessiond.read_record(sid)["permissions"] == "normal"


def test_an_unknown_profile_is_strict(monkeypatch):
    monkeypatch.setenv("MEDIA_HEADLESS_PERMISSIONS", "yolo")
    assert permissions.mode() == "strict"
    assert permissions.mode("normal") == "normal"


# --- the socket --------------------------------------------------------------------------

def test_the_socket_is_private(host):
    assert (host.sock.stat().st_mode & 0o777) == 0o600
    assert hd.call("ping")["ok"] is True
    assert hd.call("nonsense")["code"] == "bad_request"
    assert hd.call("get", session="0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0")["code"] == "not_found"


# --- the routes, over HTTP -----------------------------------------------------------------

from test_contract import AUTH, call, server, signed_in, typed  # noqa: E402,F401 — fixtures


@pytest.fixture()
def app_host(host, monkeypatch, tmp_path):
    """The canvas's routes in front of the running sessiond: no pane is live,
    a fresh chat opens in `work`, and every pane path is a recorder."""
    from agent_media_server import sessions, speech

    shelf = tmp_path / "book-tracks"
    shelf.mkdir()
    monkeypatch.setattr(sessions, "_manifest_dir", lambda: shelf)
    monkeypatch.setattr(sessions, "live_sessions", lambda: {})
    monkeypatch.setattr(sessions, "_pane_titles", lambda: {})
    monkeypatch.setattr(sessions, "_followup", lambda s: None)
    monkeypatch.setattr(sessions, "_STATES_CACHE", (0.0, []))
    monkeypatch.setenv("MEDIA_ASK_CWD", str(host.work))
    monkeypatch.setenv("MEDIA_ASK_TMUX", "amux-scratch")
    speech._NOW_CACHE.clear()
    return host


def req(*a, **k):
    res, obj = call(*a, **k)
    return res.status, obj


def _typed_into_panes(typed) -> list:
    return [t for t in typed if t[0] in ("open_window", "_send_to_pane", "panes.send")]


def test_ask_new_starts_a_headless_session_and_it_is_listed(app_host, server, signed_in, typed):
    st, body = req(server, "POST", "/ask", {"text": "reply: hi there", "target": "new"}, AUTH)
    assert st == 200 and body["ok"], body
    sid = body["session"]
    assert body["mode"] == "new" and body["how"] == "asked" and body["driver"] == "headless"
    assert body["pane"] is None and body["fresh"] is True and body["agent"] == "claude"
    assert not _typed_into_panes(typed)
    wait_for(lambda: state(app_host, sid) == "waiting")
    # Filed and voiced under the tmux session a pane would have opened in.
    assert starts(app_host)[0]["env"]["MEDIA_SOURCE_WORKSPACE"] == "amux-scratch"

    st, body = req(server, "GET", "/targets", headers=AUTH)
    row = next(r for r in body["sessions"] if r["session"] == sid)
    assert row == {"session": sid, "title": "reply: hi there", "live": True, "pane": None,
                   "recap": None, "archived": False, "rested": None, "pinned": False,
                   "driver": "headless", "drivable": True, "harness": "claude",
                   "source": "sessiond"}
    st, body = req(server, "GET", "/sessions/state", headers=AUTH)
    row = next(r for r in body["sessions"] if r["session"] == sid)
    assert row["state"] == "waiting" and row["driver"] == "headless"
    assert set(row) == {"session", "tail", "state", "mem_mb", "driver"}

    st, body = req(server, "GET", f"/conversation?session={sid}", headers=AUTH)
    assert body["live"] is True and body["pane"] is None and body["resumable"] is True


def test_the_ask_default_route_is_headless_too(app_host, server, signed_in, typed):
    st, body = req(server, "POST", "/ask", {"text": "reply: from the button"}, AUTH)
    assert st == 200 and body["how"] == "default" and body["driver"] == "headless"
    assert not _typed_into_panes(typed)


def test_reply_approval_answer_and_close_over_http(app_host, server, signed_in, typed):
    st, body = req(server, "POST", "/ask", {"text": "reply: a", "target": "new"}, AUTH)
    sid = body["session"]
    wait_for(lambda: state(app_host, sid) == "waiting")
    st, body = req(server, "POST", "/reply", {"session": sid, "text": "tool: touch x"}, AUTH)
    assert st == 200 and body == {"ok": True, "session": sid, "pane": None, "opened": False,
                                  "submitted": True, "driver": "headless", "queued": False,
                                  "acked": True, "uuid": body["uuid"]}
    wait_for(lambda: state(app_host, sid) == "approval")
    st, log = req(server, "GET", f"/conversation/log?session={sid}&messages=1", headers=AUTH)
    appr = log["approval"]
    assert appr["kind"] == "tool" and appr["tool"] == "Bash" and appr["input_summary"] == "touch x"
    st, body = req(server, "POST", "/session/answer",
                    {"session": sid, "request_id": "gone", "decision": "allow"}, AUTH)
    assert st == 409 and body["error"] == "the question has changed"
    assert body["approval"]["id"] == appr["id"]
    st, body = req(server, "POST", "/session/answer",
                    {"session": sid, "request_id": appr["id"], "decision": "allow"}, AUTH)
    assert st == 200 and body["ok"] and body["decision"] == "allow" and body["approval"] is None
    wait_for(lambda: last_text(app_host, sid) == "ran touch x")
    st, log = req(server, "GET", f"/conversation/log?session={sid}&messages=1", headers=AUTH)
    assert log["approval"] is None
    assert [m["role"] for m in log["messages"]][-2:] == ["user", "assistant"]
    st, body = req(server, "POST", "/session/close", {"session": sid}, AUTH)
    assert st == 200 and body["closed"] is True and body["pane"] is None
    wait_for(lambda: state(app_host, sid) == "closed")
    st, body = req(server, "GET", f"/conversation?session={sid}", headers=AUTH)
    assert body["live"] is False and body["resumable"] is True
    assert not _typed_into_panes(typed)


def test_flag_off_asks_open_a_pane_even_with_sessiond_running(app_host, server, signed_in,
                                                             typed, monkeypatch):
    st, body = req(server, "POST", "/ask", {"text": "reply: a", "target": "new"}, AUTH)
    sid = body["session"]
    monkeypatch.delenv("MEDIA_HEADLESS")
    st, body = req(server, "POST", "/ask", {"text": "hello", "target": "new"}, AUTH)
    assert any(t[0] == "open_window" for t in typed)       # the pane path, as before
    assert "driver" not in body
    # And a headless thread is neither listed nor driven with the flag off.
    st, body = req(server, "GET", "/targets", headers=AUTH)
    assert all(r["session"] != sid for r in body["sessions"])
    assert driver.for_session(sid).kind == "pane"


def test_a_pane_session_refuses_the_structured_answer(app_host, server, signed_in, typed,
                                                      monkeypatch):
    from agent_media_server import panes, sessions

    pane_sid = "0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0"
    monkeypatch.setattr(sessions, "live_sessions", lambda: {pane_sid: "%42"})
    monkeypatch.setattr(sessions, "approval_for", lambda p, a="claude", s="": None)
    monkeypatch.setattr(sessions, "_agent_of_pane", lambda p: "claude")
    monkeypatch.setattr(panes, "alive", lambda p: True)
    st, body = req(server, "POST", "/session/answer",
                    {"session": pane_sid, "request_id": "r", "decision": "allow"}, AUTH)
    assert st == 400 and body["error"] == "this session answers by number (choice and key)"


# --- rename -------------------------------------------------------------------------

def test_rename_reaches_a_live_headless_session_and_the_shelf_name_lists(
        app_host, server, signed_in, typed, monkeypatch, tmp_path):
    from agent_media_core import book_tracks

    shelf = tmp_path / "book-tracks"

    def rename(session, title):
        # What book_tracks.rename keeps: the manifest's title (the folder
        # keeps its first name).
        (shelf / f"{session}.json").write_text(json.dumps(
            {"session": session, "folder": str(tmp_path / "Conversations" / "First name"),
             "title": title}))
        return title

    monkeypatch.setattr(book_tracks, "rename", rename)
    st, body = req(server, "POST", "/ask", {"text": "reply: hi", "target": "new"}, AUTH)
    sid = body["session"]
    wait_for(lambda: state(app_host, sid) == "waiting")
    turns = app_host.sup.get(sid)["turns"]
    st, body = req(server, "POST", "/rename", {"session": sid, "title": "Better name"}, AUTH)
    assert st == 200 and body == {"ok": True, "session": sid, "title": "Better name",
                                  "terminal": True, "why": None}
    wait_for(lambda: last_text(app_host, sid) == "Session renamed to: Better name")
    assert not _typed_into_panes(typed)
    # Not a turn, and not the listener's words.
    assert app_host.sup.get(sid)["turns"] == turns
    assert not [t for t in app_host.turns if "rename" in t[1]]
    st, body = req(server, "GET", "/targets", headers=AUTH)
    row = next(r for r in body["sessions"] if r["session"] == sid)
    assert row["title"] == "Better name"


def test_rename_of_a_parked_headless_session_says_when_it_lands(
        app_host, server, signed_in, typed, monkeypatch):
    from agent_media_core import book_tracks

    monkeypatch.setattr(book_tracks, "rename", lambda s, t: t)
    sid = start(app_host, "reply: pineapple")
    wait_for(lambda: state(app_host, sid) == "waiting")
    monkeypatch.setenv("MEDIA_SESSIOND_IDLE", "0")
    app_host.sup.park_idle(time.time() + 5)
    wait_for(lambda: state(app_host, sid) == "parked")
    n = len(starts(app_host))
    st, body = req(server, "POST", "/rename", {"session": sid, "title": "Later"}, AUTH)
    assert st == 200 and body["terminal"] is False
    assert body["why"] == "headless sessions pick up the name on their next resume"
    # Not woken for it.
    assert state(app_host, sid) == "parked" and len(starts(app_host)) == n
    assert not _typed_into_panes(typed)
