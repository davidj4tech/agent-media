"""Codex's and pi's conversations: where they are, what they are called."""

import json
import os

import pytest

from agent_media_core import activity, harnesses

CX = "01a0a6f1-ed4b-7f91-8d63-a61653a846f9"
PI = "5cee9d00-6a28-4d77-9942-bb3ec54d096c"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    day = tmp_path / "codex" / "sessions" / "2026" / "09" / "16"
    day.mkdir(parents=True)
    rows = [
        {"type": "session_meta", "payload": {"id": CX, "cwd": "/home/x/scratch"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "# AGENTS.md instructions"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "What are the\ntwo startup issues?"}]}},
    ]
    (day / f"rollout-2026-09-16T07-21-07-{CX}.jsonl").write_text("\n".join(map(json.dumps, rows)))
    (tmp_path / "codex" / "session_index.jsonl").write_text(
        json.dumps({"id": CX, "thread_name": "Old name"}) + "\n"
        + json.dumps({"id": CX, "thread_name": "Identify two startup issues"}) + "\n")
    sess = tmp_path / "pi" / "sessions" / "--home-x-scratch--"
    sess.mkdir(parents=True)
    rows = [
        {"type": "session", "id": PI, "cwd": "/home/x/scratch"},
        {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "play my book"}]}},
        {"type": "session_info", "name": "Audiobookshelf via search"},
    ]
    (sess / f"2026-09-19T21-29-33-432Z_{PI}.jsonl").write_text("\n".join(map(json.dumps, rows)))
    return tmp_path


def test_each_agent_is_found_by_its_own_file(homes):
    assert harnesses.harness_of(CX) == "codex"
    assert harnesses.harness_of(PI) == "pi"
    assert harnesses.harness_of("11111111-2222-3333-4444-555555555555") == ""
    assert harnesses.harness_of("../../etc") == ""


def test_cwd_title_and_question(homes):
    assert harnesses.cwd_of(CX) == harnesses.cwd_of(PI) == "/home/x/scratch"
    assert harnesses.title_of(CX) == "Identify two startup issues"     # the last name wins
    assert harnesses.title_of(PI) == "Audiobookshelf via search"
    assert harnesses.first_prompt(CX) == "What are the two startup issues?"   # not the preamble
    assert harnesses.first_prompt(PI) == "play my book"


def test_the_library_names_a_codex_thread_by_its_title(homes):
    from agent_media_core import session_feed
    assert session_feed.asked_for(CX, []) == "Identify two startup issues"


def test_resume_and_fresh_arguments():
    assert harnesses.resume_argv("codex", CX) == ["resume", CX]
    assert harnesses.resume_argv("pi", PI) == ["--session", PI]
    assert harnesses.resume_argv("claude", PI) == ["--resume", PI]
    assert harnesses.fresh_argv("pi", PI) == ["--session-id", PI]
    assert harnesses.fresh_argv("codex", CX) == []


def test_a_pi_registration_holds_only_while_that_pi_is_in_that_pane(homes, monkeypatch):
    assert harnesses.register_pane("pi", PI, 4242, "%7")
    assert not harnesses.register_pane("pi", "not-a-uuid", 4242, "%7")
    monkeypatch.setattr(harnesses, "_argv", lambda pid: ["pi"])
    monkeypatch.setattr(harnesses, "_pane_of", lambda pid: "%7")
    assert [(r.session, r.pane) for r in harnesses._registered()] == [(PI, "%7")]
    monkeypatch.setattr(harnesses, "_pane_of", lambda pid: "%8")      # pid reused elsewhere
    assert harnesses._registered() == []
    monkeypatch.setattr(harnesses, "_pane_of", lambda pid: "%7")
    monkeypatch.setattr(harnesses, "_argv", lambda pid: ["zsh"])      # no longer a pi
    assert harnesses._registered() == []


@pytest.mark.parametrize("tool,args,want", [
    ("bash", {"command": "ls -la"}, "List files"),
    ("read", {"path": "/a/b/canvas.py"}, "Read canvas.py"),
    ("edit", {"path": "/a/x.ts"}, "Edit x.ts"),
    ("ls", {"path": "/home/x/projects"}, "List projects"),
    ("exec_command", {"command": ["bash", "-lc", "pytest -q"]}, "Run the tests"),
    ("apply_patch", {"input": "*** Begin Patch\n*** Update File: src/a.py\n@@"}, "Edit a.py"),
    ("update_plan", {}, ""),
])
def test_codex_and_pi_tools_are_named(tool, args, want):
    assert activity.describe(tool, args) == want


def test_pi_events_reach_the_step_list_and_the_registry(homes, monkeypatch):
    from agent_media_core.intake import agent_events
    monkeypatch.setenv("TMUX_PANE", "%5")
    agent_events.handle({"hook_event_name": "SessionStart", "session_id": PI, "pid": 99}, "pi")
    assert json.loads((harnesses.registry_dir() / "5").read_text())["session"] == PI
    agent_events.handle({"hook_event_name": "PreToolUse", "session_id": PI,
                         "tool_name": "read", "tool_input": {"path": "/x/a.py"}}, "pi")
    rows = (activity.activity_dir() / f"{PI}.jsonl").read_text().splitlines()
    assert json.loads(rows[-1])["text"] == "Read a.py"
