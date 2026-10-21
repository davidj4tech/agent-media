"""Thinking speech preference and watcher: fake submissions, no real audio."""
import json
from types import SimpleNamespace

import pytest

from agent_media_server import app, auth, sessions, thinking_speech as T, transcript
from agent_media_core import sealed, speak_priority

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def env(monkeypatch, tmp_path):
    import os
    for key in list(os.environ):
        if key.startswith(("MEDIA_", "RELAY_")):
            monkeypatch.delenv(key, raising=False)
    for key in ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    path = tmp_path / "transcript.jsonl"
    path.write_text("")
    watcher = T.Watcher()
    monkeypatch.setattr(T, "_WATCHER", watcher)
    monkeypatch.setattr(auth, "gate", lambda b: ({"id": "u"}, {}))
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%7"})
    monkeypatch.setattr(sessions, "agent_of", lambda s: "claude")
    monkeypatch.setattr(transcript, "transcript_of", lambda s: ("claude", str(path)))
    working = [True]
    monkeypatch.setattr(sessions, "activity_of", lambda *a: {"state": "working" if working[0] else "waiting"})
    messages = [{"id": "m", "role": "assistant", "parts": [], "turn": {"running": True}}]
    monkeypatch.setattr(transcript, "messages", lambda *a, **kw: (messages, False))
    monkeypatch.setattr(T.toast, "should_hold", lambda s: False)
    clock = [10.0]
    monkeypatch.setattr(T.time, "monotonic", lambda: clock[0])
    calls = []
    def launch(*args):
        calls.append(args)
        watcher.busy = False
    monkeypatch.setattr(watcher, "launch", launch)
    return SimpleNamespace(watcher=watcher, path=path, messages=messages,
                           calls=calls, clock=clock, working=working)


def enable():
    return T.setting(SID, "t", body={"enabled": True})


def thought(env, text="A readable thought", **extra):
    env.messages[0]["parts"].append({"type": "reasoning", "text": text, **extra})
    with env.path.open("a") as f:
        f.write("a new record\n")


def settle(env):
    env.watcher.tick()
    env.clock[0] += T.SETTLE_S
    env.watcher.tick()


def test_preference_defaults_off_persists_and_is_idempotent(env):
    assert T.setting(SID, "t")[1] == {"session": SID, "enabled": False, "supported": True}
    assert enable()[1]["enabled"] is True
    token = T._PREFS.get(SID)
    assert json.loads(T._PREFS.path().read_text())[SID] == token
    assert enable()[0] and T._PREFS.get(SID) == token
    assert T.setting(SID, "t", body={"enabled": False})[1]["enabled"] is False
    assert SID not in T._PREFS.all()


def test_validation_auth_and_missing_session(env, monkeypatch):
    assert T.setting("bad", "t")[1]["status"] == 400
    for value in (None, 1, "true", [], {}):
        assert T.setting(SID, "t", body={"enabled": value})[1]["status"] == 400
    monkeypatch.setattr(auth, "gate", lambda b: (None, {"status": 403}))
    assert enable()[1]["status"] == 403
    monkeypatch.setattr(auth, "gate", lambda b: ({"id": "u"}, {}))
    monkeypatch.setattr(sessions, "live_sessions", lambda: {})
    monkeypatch.setattr(sessions, "session_exists", lambda s: False)
    monkeypatch.setattr(sessions, "_folder_for_session", lambda s: "")
    assert enable()[1]["status"] == 404
    assert "/session/thinking-speech" in app.CORS_PATHS


def test_unsupported_agent_can_read_or_disable_but_not_enable(env, monkeypatch):
    monkeypatch.setattr(transcript, "transcript_of", lambda s: ("", ""))
    monkeypatch.setattr(sessions, "agent_of", lambda s: "hermes")
    assert T.setting(SID, "t")[1]["supported"] is False
    assert enable()[1]["status"] == 409
    assert T.setting(SID, "t", body={"enabled": False})[0]


def test_enable_skips_history_and_deduplicates_new_parts(env):
    thought(env, "Old thinking")
    enable()
    settle(env)
    assert env.calls == []
    thought(env, "New thinking")
    settle(env)
    assert [c[-1] for c in env.calls] == ["New thinking"]
    settle(env)
    enable()  # another device enabling it again must not reset the cursor
    settle(env)
    assert len(env.calls) == 1


def test_empty_redacted_and_normal_text_are_never_spoken(env):
    enable()
    thought(env, "")
    thought(env, "unavailable", redacted=True)
    env.messages[0]["parts"].append({"type": "text", "text": "A normal reply"})
    settle(env)
    assert env.calls == []


def test_updates_settle_once_without_repeating_a_replaced_block(env):
    enable()
    thought(env, "Partial")
    env.watcher.tick()
    env.messages[0]["parts"][0]["text"] = "Complete thinking"
    settle(env)
    assert [c[-1] for c in env.calls] == ["Complete thinking"]
    env.messages[0]["parts"][0]["text"] += " changed again"
    settle(env)
    assert len(env.calls) == 1


@pytest.mark.parametrize("cause", ["busy", "quiet", "unwatched", "ended", "sealed"])
def test_unheard_thinking_is_consumed_not_queued(env, monkeypatch, cause):
    enable()
    env.watcher.busy = cause == "busy"
    speak_priority.set_level(SID, "quiet" if cause == "quiet" else "normal")
    monkeypatch.setattr(T.toast, "should_hold", lambda s: cause == "unwatched")
    monkeypatch.setattr(sealed, "is_sealed", lambda s: cause == "sealed")
    env.working[0] = cause != "ended"
    thought(env)
    settle(env)
    assert env.calls == []
    env.watcher.busy = False
    speak_priority.set_level(SID, "auto")
    monkeypatch.setattr(sealed, "is_sealed", lambda s: False)
    env.working[0] = True
    settle(env)
    assert env.calls == []


@pytest.mark.parametrize("change", ["restart", "replace", "truncate", "toggle"])
def test_new_baseline_never_replays_old_thinking(env, change):
    enable()
    thought(env)
    settle(env)
    env.calls.clear()
    thought(env, "Before new baseline")
    if change == "restart":
        env.watcher.cursors.clear()
    elif change == "replace":
        new = env.path.with_suffix(".new")
        new.write_text("replacement\n")
        new.replace(env.path)
    elif change == "truncate":
        env.path.write_text("")
    else:
        T.setting(SID, "t", body={"enabled": False})
        enable()
    settle(env)
    assert env.calls == []


def test_cancellation_checks_preference_generation_and_running_turn(env):
    enable()
    token = T._PREFS.get(SID)
    assert env.watcher.allowed(SID, token, "m")
    env.working[0] = False
    assert not env.watcher.allowed(SID, token, "m")
    env.working[0] = True
    T.setting(SID, "t", body={"enabled": False})
    enable()
    assert not env.watcher.allowed(SID, token, "m")


def test_submission_uses_source_part_and_cancellable_low_priority(env, monkeypatch):
    from agent_media_core.intake import submit
    calls = []
    monkeypatch.setattr(submit, "submit_event", lambda e, **kw: calls.append((e, kw)))
    enable()
    env.watcher.speak(SID, T._PREFS.get(SID), ("m", 2), "Thinking words")
    event, kwargs = calls[0]
    assert event.priority.value == "low"
    assert event.metadata["thinking_message"] == "m"
    assert event.metadata["thinking_part"] == 2
    assert kwargs["playback_allowed"]()
    T.setting(SID, "t", body={"enabled": False})
    assert not kwargs["playback_allowed"]()


def test_http_routes_return_the_documented_envelope(env, monkeypatch):
    import io
    replies = []
    monkeypatch.setattr(app, "_json", lambda h, status, body: replies.append((status, body)))
    body = json.dumps({"session": SID, "enabled": True}).encode()
    handler = SimpleNamespace(path="/session/thinking-speech", rfile=io.BytesIO(body),
                              headers={"Authorization": "Bearer t", "Content-Length": str(len(body))})
    assert app._post(handler, "/session/thinking-speech")
    assert replies[-1] == (200, {"ok": True, "session": SID, "enabled": True, "supported": True})
    handler.path += f"?session={SID}"
    assert app._get(handler, "/session/thinking-speech")
    assert replies[-1][1]["enabled"] is True


@pytest.mark.parametrize("harness", ["claude", "codex", "pi"])
def test_real_transcript_readers_supply_new_thinking(env, monkeypatch, harness):
    monkeypatch.setattr(transcript, "transcript_of", lambda s: (harness, str(env.path)))
    monkeypatch.setattr(transcript, "messages", lambda *a, **kw:
                        transcript.messages_at(str(env.path), harness=harness, **kw))
    enable()
    if harness == "claude":
        record = {"type": "assistant", "uuid": "a", "message": {
            "role": "assistant", "content": [{"type": "thinking", "thinking": "Readable thinking"}],
            "stop_reason": "tool_use"}}
    elif harness == "codex":
        record = {"type": "response_item", "timestamp": "2026-10-21T00:00:00Z",
                  "payload": {"type": "reasoning", "summary": [
                      {"type": "summary_text", "text": "Readable thinking"}]}}
    else:
        record = {"type": "message", "id": "a", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Readable thinking"}]}}
    env.path.write_text(json.dumps(record, separators=(",", ":")) + "\n")
    parsed = transcript.messages(SID, limit=30)[0]
    assert T._parts(parsed), parsed
    settle(env)
    assert [c[-1] for c in env.calls] == ["Readable thinking"]
