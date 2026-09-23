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
        "turn-id": "turn-456", "cwd": "/tmp",
        "input-messages": ["Do not speak this"],
    })]) == 0
    assert len(events) == 1
    assert events[0].text == "Hello world"
    assert events[0].source == Source.CODEX
    assert events[0].metadata == {
        "kind": "stop", "session": "01a0a6f1-ed4b-7f91-8d63-a61653a846f9",
        "turn_id": "turn-456", "cwd": "/tmp",
    }


def test_thread_id_that_is_not_a_session_is_dropped(events):
    """A thread shelved under a non-session id is one no route can address:
    `/conversation`, `/session/archive` and `/reply` all answer 400."""
    assert codex.main([json.dumps({
        "type": "agent-turn-complete", "last-assistant-message": "Hello world",
        "thread-id": "codex-adapter-verification", "cwd": "/tmp",
    })]) == 0
    assert events[0].text == "Hello world"
    assert "session" not in events[0].metadata
    assert events[0].metadata["cwd"] == "/tmp"


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
