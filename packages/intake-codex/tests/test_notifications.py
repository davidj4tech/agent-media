import io
import json

import pytest

import agent_media_intake_codex as codex
from agent_media_core.intake import _hook_stdin
from agent_media_core.types import Source


@pytest.fixture
def events(monkeypatch):
    captured = []
    monkeypatch.setenv("MEDIA_HOOK_ENABLED", "1")
    monkeypatch.setenv("CODEX_TTS_ENABLED", "1")
    monkeypatch.setattr(_hook_stdin, "_load_env_file", lambda _: None)
    monkeypatch.setattr(_hook_stdin, "submit_event", captured.append)
    return captured


def test_notification_metadata_and_text(events):
    assert codex.main([json.dumps({
        "type": "agent-turn-complete", "last-assistant-message": "Hello world",
        "thread-id": "01a0a6f1-ed4b-7f91-8d63-a61653a846f9",
        "turn-id": "turn-456", "cwd": "/home/x/work",
        "input-messages": ["Do not speak this"],
    })]) == 0
    assert len(events) == 1
    assert events[0].text == "Hello world"
    assert events[0].source == Source.CODEX
    assert events[0].metadata == {
        "kind": "stop", "session": "01a0a6f1-ed4b-7f91-8d63-a61653a846f9",
        "turn_id": "turn-456", "cwd": "/home/x/work",
    }


def test_thread_id_that_is_not_a_session_is_dropped(events):
    """A thread shelved under a non-session id is one no route can address:
    `/conversation`, `/session/archive` and `/reply` all answer 400."""
    assert codex.main([json.dumps({
        "type": "agent-turn-complete", "last-assistant-message": "Hello world",
        "thread-id": "codex-adapter-verification", "cwd": "/home/x/work",
    })]) == 0
    assert events[0].text == "Hello world"
    assert "session" not in events[0].metadata
    assert events[0].metadata["cwd"] == "/home/x/work"


def test_plain_stdin(events, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("Legacy speech"))
    assert codex.main([]) == 0
    assert events[0].text == "Legacy speech"


@pytest.mark.parametrize("payload", [
    {"type": "approval-requested", "last-assistant-message": "Ignore"},
    {"type": "agent-turn-complete"},
    {"type": "agent-turn-complete", "last-assistant-message": None},
    {"type": "agent-turn-complete", "last-assistant-message": "  "},
    {"type": "agent-turn-complete", "last-assistant-message": {"bad": "value"}},
])
def test_ignored_notifications(events, payload):
    assert codex.main([json.dumps(payload)]) == 0
    assert events == []


@pytest.mark.parametrize("raw", ["not json", "null", "[]"])
def test_invalid_notification(events, raw):
    assert codex.main([raw]) == 1
    assert events == []


def test_disabled(events, monkeypatch):
    monkeypatch.setenv("CODEX_TTS_ENABLED", "0")
    assert codex.main([json.dumps({
        "type": "agent-turn-complete", "last-assistant-message": "Silent",
    })]) == 0
    assert events == []


def test_a_title_naming_turn_is_not_spoken(events):
    """Codex names a thread with a hidden turn that answers in JSON."""
    assert codex.main([json.dumps({
        "type": "agent-turn-complete", "last-assistant-message": '{"title":"Run sleep command"}',
        "thread-id": "01a0bb92-834d-7db3-97be-62da468f2f0a",
    })]) == 0
    assert events == []


def test_event_mode_hands_stdin_to_the_shared_handler(monkeypatch):
    seen = []
    import agent_media_core.intake.agent_events as ev
    monkeypatch.setattr(ev, "handle", lambda payload, harness: seen.append((payload, harness)) or 0)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"hook_event_name": "UserPromptSubmit", "session_id": "s"}'))
    assert codex.main(["event"]) == 0
    assert seen == [({"hook_event_name": "UserPromptSubmit", "session_id": "s"}, "codex")]


def test_a_scripted_run_is_not_spoken(events, monkeypatch):
    """`codex exec` and runs from /tmp are a script's: never spoken or
    shelved (the Cloudflare DNS runs of 25 Sep 2026)."""
    from agent_media_core import harnesses

    turn = {"type": "agent-turn-complete", "last-assistant-message": "Zone ID: 84d7…"}
    assert codex.main([json.dumps({**turn, "cwd": "/tmp",
                                   "thread-id": "01a0d3d4-9c5f-7ea0-a7d6-060e7d7fcc2f"})]) == 0
    monkeypatch.setattr(harnesses, "codex_run_scripted", lambda s, cwd="": s.startswith("01a0d3d5"))
    assert codex.main([json.dumps({**turn, "cwd": "/home/x/site",
                                   "thread-id": "01a0d3d5-7e07-7bd1-b24d-b33de540e3ef"})]) == 0
    assert events == []


def test_a_scripted_runs_prompt_is_not_recorded(monkeypatch):
    from agent_media_core.intake import agent_events, hook_claude_code

    seen = []
    monkeypatch.setattr(hook_claude_code, "_handle_user_prompt", lambda p: seen.append(p) or 0)
    agent_events.handle({"hook_event_name": "UserPromptSubmit", "cwd": "/tmp",
                         "session_id": "01a0d3d4-9c5f-7ea0-a7d6-060e7d7fcc2f", "prompt": "x"}, "codex")
    assert seen == []
