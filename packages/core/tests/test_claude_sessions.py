"""Claude Code's own session files name the session in a pane."""

import json
import os

from agent_media_core import claude_sessions

SID = "027c24b1-a031-44a8-97e4-f890c45494d4"


def _write(tmp_path, pid, **data):
    (tmp_path / f"{pid}.json").write_text(json.dumps({"pid": pid, **data}))


def test_a_live_claude_is_found_by_pane(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSIONS_DIR", str(tmp_path))
    monkeypatch.setattr(claude_sessions, "_is_claude", lambda pid: pid == 42)
    _write(tmp_path, 42, sessionId=SID, tmux="p-runlet:@489.%518")
    _write(tmp_path, 43, sessionId="dead", tmux="x:@1.%9")   # pid gone or reused
    _write(tmp_path, 44, sessionId="headless")                 # no pane
    (tmp_path / "45.json").write_text("{not json")
    assert claude_sessions.by_pane() == {"%518": SID}
    assert claude_sessions.session_for_pid(42) == SID
    assert claude_sessions.session_for_pid(99) is None


def test_only_a_claude_process_counts():
    assert not claude_sessions._is_claude(os.getpid())   # pytest is python


def test_running_includes_a_claude_outside_tmux(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSIONS_DIR", str(tmp_path))
    monkeypatch.setattr(claude_sessions, "_is_claude", lambda pid: True)
    _write(tmp_path, 42, sessionId=SID, tmux="p:@1.%5")
    _write(tmp_path, 43, sessionId="other")
    assert sorted(claude_sessions.running()) == [(42, SID, "%5"), (43, "other", "")]
