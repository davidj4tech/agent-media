"""The usage limit's record (quota.py) and the desk hook that writes it
(intake/hook_claude_code.py StopFailure), 20 Oct 2026. Throwaway state and
transcripts only: nothing reaches a pane, a session or the phone."""

from __future__ import annotations

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent_media_core import quota

MEL = ZoneInfo("Australia/Melbourne")
SAID = "You've hit your session limit · resets 2am (Australia/Melbourne)"


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MEDIA_QUOTA_RESUME_MARGIN_S", "60")
    monkeypatch.setenv("MEDIA_QUOTA_RESUME_GAP_S", "20")
    monkeypatch.delenv("MEDIA_SESSIOND_SESSION", raising=False)


def _mel(*a) -> float:
    return datetime(*a, tzinfo=MEL).timestamp()


def test_the_reset_is_read_from_the_sentence_in_its_zone():
    now = _mel(2026, 10, 20, 0, 53)
    assert quota.parse_reset(SAID, now) == _mel(2026, 10, 20, 2, 0)
    # Past today's: tomorrow's.
    assert quota.parse_reset("resets 4pm (Australia/Melbourne)",
                             _mel(2026, 10, 20, 17, 0)) == _mel(2026, 10, 21, 16, 0)
    assert quota.parse_reset("resets 3:30pm (Australia/Melbourne)", now) \
        == _mel(2026, 10, 20, 15, 30)
    assert quota.parse_reset("resets Oct 22, 9am (Australia/Melbourne)", now) \
        == _mel(2026, 10, 22, 9, 0)
    assert quota.parse_reset("You've hit your limit", now) is None


def test_a_limit_result_is_told_from_other_errors():
    assert quota.is_limit_result({"type": "result", "is_error": True,
                                  "api_error": "usage_limit_reached", "result": SAID})
    assert quota.is_limit_result({"type": "result", "is_error": True, "api_error_status": 429,
                                  "terminal_reason": "api_error", "result": SAID})
    assert not quota.is_limit_result({"type": "result", "is_error": True,
                                      "terminal_reason": "aborted_tools", "result": ""})
    assert not quota.is_limit_result({"type": "result", "is_error": False, "result": "ok"})


def test_sessions_wait_in_order_and_are_taken_one_gap_apart():
    t0 = 1_000_000.0
    quota.note_limit({"kind": "headless", "session": "b", "at": t0 + 5}, t0 + 100, now=t0 + 5)
    quota.note_limit({"kind": "headless", "session": "a", "at": t0}, t0 + 100, now=t0 + 6)
    # Again for one already waiting: its place and earliest time are kept.
    quota.note_limit({"kind": "headless", "session": "a", "at": t0 + 7}, t0 + 100, now=t0 + 7)
    rec = quota.read()
    assert [w["session"] for w in rec["waiting"]] == ["a", "b"]
    assert rec["waiting"][0]["at"] == t0
    assert quota.limited(t0 + 150) and quota.view(t0 + 150)["state"] == "waiting"
    assert quota.take_next(t0 + 150) is None          # within the margin
    assert quota.take_next(t0 + 161)["session"] == "a"
    assert quota.limited(t0 + 161) is None
    assert quota.session_view("b", t0 + 161)["state"] == "resuming"
    assert quota.take_next(t0 + 170) is None          # the gap
    assert quota.take_next(t0 + 182)["session"] == "b"
    assert quota.take_next(t0 + 300) is None and quota.read() is None


def test_one_that_hits_it_again_moves_the_reset_for_the_rest():
    t0 = 1_000_000.0
    for i, sid in enumerate(("a", "b")):
        quota.note_limit({"kind": "headless", "session": sid, "at": t0 + i}, t0 + 100, now=t0)
    assert quota.take_next(t0 + 200)["session"] == "a"
    quota.note_limit({"kind": "headless", "session": "a", "at": t0 + 205}, t0 + 5000,
                     now=t0 + 205)
    rec = quota.read()
    assert rec["state"] == "limited" and rec["resets_at"] == t0 + 5000
    assert [w["session"] for w in rec["waiting"]] == ["b", "a"]
    assert quota.take_next(t0 + 300) is None


def test_no_reset_known_is_a_backoff_that_grows():
    t0 = 1_000_000.0
    quota.note_limit({"kind": "headless", "session": "a"}, None, now=t0)
    assert quota.read()["resets_at"] == t0 + 900 and quota.read()["source"] == "backoff"
    assert quota.take_next(t0 + 1000)["session"] == "a"
    quota.note_limit({"kind": "headless", "session": "a"}, None, now=t0 + 1001)
    assert quota.read()["resets_at"] == t0 + 1001 + 1800


def test_an_answer_that_went_through_lifts_it_early():
    now = time.time()
    quota.note_limit({"kind": "headless", "session": "a"}, now + 3600, now=now - 120)
    assert quota.limited(now)
    assert quota.lifted(now)
    assert quota.limited(now) is None
    assert quota.take_next(now)["session"] == "a"


def _line(**kw) -> str:
    return json.dumps(kw) + "\n"


def _limit_line(reset: int) -> str:
    return _line(type="assistant", isApiErrorMessage=True, apiError="usage_limit_reached",
                 timestamp="2026-10-19T13:53:43.529Z",
                 apiErrorParams={"rate_limit_info": {"status": "rejected", "resetsAt": reset,
                                                     "rateLimitType": "five_hour"}},
                 message={"role": "assistant", "content": [{"type": "text", "text": SAID}]})


@pytest.fixture
def desk(tmp_path):
    proj = tmp_path / "projects" / "-w"
    sub = proj / "s1" / "subagents"
    sub.mkdir(parents=True)
    tp = proj / "s1.jsonl"
    tp.write_text(_line(type="user", message={"role": "user", "content": "fix the build"})
                  + _limit_line(1792422000))
    # One agent stopped by the limit, one that finished, one continued since.
    (sub / "agent-a1.jsonl").write_text(_line(type="user", isSidechain=True,
                                              message={"content": "go"})
                                        + _limit_line(1792422000))
    (sub / "agent-a1.meta.json").write_text(json.dumps({"description": "Write the docs"}))
    (sub / "agent-a2.jsonl").write_text(_line(type="assistant", message={"content": "done"}))
    (sub / "agent-a3.jsonl").write_text(_limit_line(1792422000)
                                        + _line(type="user", message={"content": "go on"}))
    return tp


def test_a_transcript_says_when_it_ended_on_the_limit(desk):
    hit = quota.transcript_limit(desk)
    assert hit["resets_at"] == 1792422000 and hit["kind"] == "five_hour"
    assert quota.last_user_text(desk) == "fix the build"
    got = quota.limited_agents(desk.with_suffix("") / "subagents")
    assert [(a["id"], a["description"]) for a in got] == [("a1", "Write the docs")]


def _hook(payload: dict, monkeypatch) -> None:
    import io

    from agent_media_core.intake import hook_claude_code as h

    monkeypatch.setattr(h, "load_env_file", lambda *a, **k: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert h.main() == 0


def test_the_desk_hook_puts_a_limited_session_in_line(desk, monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setenv("CLAUDE_TTS_ENABLED", "0")      # speech off still records it
    _hook({"hook_event_name": "StopFailure", "session_id": "s1", "cwd": "/w",
           "transcript_path": str(desk), "error": "rate_limit",
           "last_assistant_message": SAID}, monkeypatch)
    rec = quota.read()
    assert rec["resets_at"] == 1792422000
    (w,) = rec["waiting"]
    assert w["kind"] == "desk" and w["pane"] == "%7" and w["cwd"] == "/w"
    assert w["last_text"] == "fix the build" and w["turn"] is True
    assert [t["id"] for t in w["tasks"]] == ["a1"]


def test_the_desk_hook_leaves_other_errors_and_headless_sessions(desk, monkeypatch, tmp_path):
    other = tmp_path / "o.jsonl"
    other.write_text(_line(type="assistant", isApiErrorMessage=True,
                           message={"content": [{"type": "text", "text": "Overloaded"}]}))
    _hook({"hook_event_name": "StopFailure", "session_id": "s2", "transcript_path": str(other),
           "error": "server_error", "last_assistant_message": "Overloaded"}, monkeypatch)
    assert quota.read() is None
    monkeypatch.setenv("MEDIA_SESSIOND_SESSION", "s1")
    _hook({"hook_event_name": "StopFailure", "session_id": "s1",
           "transcript_path": str(desk), "error": "rate_limit"}, monkeypatch)
    assert quota.read() is None


def test_setup_registers_the_stop_failure_hook():
    from agent_media_core import setup

    assert ("StopFailure", 30, True, "rate_limit") in setup.CLAUDE_HOOK_SPEC
