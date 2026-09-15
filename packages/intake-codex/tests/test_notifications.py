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
        "thread-id": "session-123", "turn-id": "turn-456", "cwd": "/tmp",
        "input-messages": ["Do not speak this"],
    })]) == 0
    assert len(events) == 1
    assert events[0].text == "Hello world"
    assert events[0].source == Source.CODEX
    assert events[0].metadata == {
        "kind": "stop", "session": "session-123", "turn_id": "turn-456", "cwd": "/tmp",
    }


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
