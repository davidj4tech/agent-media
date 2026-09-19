"""What a session is doing: recorded by hooks, read back per reply."""

from __future__ import annotations

import pytest

from agent_media_core import activity

SID = "6c73498c-02c1-4846-8350-a82006973571"


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def _ev(name, at, **kw):
    activity.record({"session_id": SID, "hook_event_name": name, **kw}, now=at)


@pytest.mark.parametrize("tool,args,want", [
    ("Bash", {"command": "ls -la", "description": "List files"}, "List files"),
    ("Bash", {"command": "ls -la"}, "List files"),
    ("Bash", {"command": "ls -t ~/.local/state/agent-media/activity"}, "List activity"),
    ("Bash", {"command": "cd ~/x && tok=$(cat f) && git push -q"}, "Run git push -q"),
    ("Bash", {"command": "cd ~/x; set -a; pytest -q tests | tail -3"}, "Run the tests"),
    ("Bash", {"command": "f=$(ls -t | head -1); tail -15 \"$f\""}, "Run tail -15 \"$f\""),
    ("Bash", {"command": "sed -n 40,75p mixins/bookshelfCards.js"}, "Read bookshelfCards.js"),
    ("Bash", {"command": "git grep -n \"def _clip_rows\" pkg/cli.py"}, "Search for def _clip_rows"),
    ("Bash", {"command": "git log --oneline -5 -- a.py"}, "Check git log"),
    ("Bash", {"command": "python3 - <<'EOF'\nprint(1)\nEOF"}, "Run a Python script"),
    ("Bash", {"command": "make deploy"}, "Run make deploy"),
    ("Read", {"file_path": "/a/b/canvas.py"}, "Read canvas.py"),
    ("Edit", {"file_path": "/a/SpeechBar.vue"}, "Edit SpeechBar.vue"),
    ("Grep", {"pattern": "speech/now"}, "Search for speech/now"),
    ("WebFetch", {"url": "https://example.com/x"}, "Read example.com"),
    ("mcp__media__music_play", {}, "media music play"),
    ("TodoWrite", {}, ""),
])
def test_describe(tool, args, want):
    assert activity.describe(tool, args) == want


def test_a_finished_turn_lands_on_its_reply():
    _ev("UserPromptSubmit", 100)
    _ev("PreToolUse", 101, tool_name="Read", tool_input={"file_path": "/x/a.py"})
    _ev("PreToolUse", 110, tool_name="Bash", tool_input={"command": "x", "description": "Run the tests"})
    _ev("Stop", 160)
    lines = [{"who": "you", "at": 99.5}, {"who": "agent", "at": 161}]
    assert activity.attach(SID, lines) is None
    assert lines[1]["work"] == {"seconds": 60.0, "count": 2,
                                "steps": ["Read a.py", "Run the tests"]}
    assert "work" not in lines[0]


def test_a_running_turn_says_what_it_is_doing():
    _ev("UserPromptSubmit", 100)
    _ev("PreToolUse", 103, tool_name="Grep", tool_input={"pattern": "foo"})
    working = activity.attach(SID, [{"who": "you", "at": 99.5}])
    assert working["since"] == 100 and working["count"] == 1
    assert working["current"] == "Search for foo"
    assert working["steps"] == ["Search for foo"]


def test_each_turn_goes_to_one_reply():
    _ev("UserPromptSubmit", 100)
    _ev("PreToolUse", 101, tool_name="Read", tool_input={"file_path": "/x/a.py"})
    _ev("Stop", 120)
    lines = [{"who": "agent", "at": 121}, {"who": "agent", "at": 130}]
    activity.attach(SID, lines)
    assert "work" in lines[0] and "work" not in lines[1]


def test_a_reply_before_the_turn_ends_is_not_given_it():
    _ev("UserPromptSubmit", 100)
    _ev("PreToolUse", 101, tool_name="Read", tool_input={"file_path": "/x/a.py"})
    _ev("Stop", 200)
    lines = [{"who": "agent", "at": 150}]   # a notification spoken mid-turn
    activity.attach(SID, lines)
    assert "work" not in lines[0]


def test_bad_sessions_and_events_write_nothing(tmp_path):
    activity.record({"session_id": "../etc", "hook_event_name": "Stop"})
    activity.record({"session_id": SID, "hook_event_name": "Notification"})
    assert not activity.activity_dir().exists() or not any(activity.activity_dir().iterdir())


def test_the_file_is_trimmed(monkeypatch):
    monkeypatch.setattr(activity, "MAX_BYTES", 2000)
    monkeypatch.setattr(activity, "KEEP_LINES", 10)
    for i in range(100):
        _ev("PreToolUse", i, tool_name="Read", tool_input={"file_path": f"/x/{i}.py"})
    assert len(activity._path(SID).read_text().splitlines()) <= 40
