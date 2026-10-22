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
    assert T.setting(SID, "t")[1] == {"session": SID, "enabled": False, "supported": True,
                                     "own": False, "default": False}
    assert enable()[1]["enabled"] is True
    token = T._PREFS.get(SID)
    assert json.loads(T._PREFS.path().read_text())[SID] == token
    assert enable()[0] and T._PREFS.get(SID) == token
    assert T.setting(SID, "t", body={"enabled": False})[1]["enabled"] is False
    assert T._PREFS.get(SID) is False


def test_validation_auth_and_missing_session(env, monkeypatch):
    assert T.setting("bad", "t")[1]["status"] == 400
    for value in (1, "true", [], {}):
        assert T.setting(SID, "t", body={"enabled": value})[1]["status"] == 400
    assert T.setting(SID, "t", body={})[1]["status"] == 400
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
    assert replies[-1] == (200, {"ok": True, "session": SID, "enabled": True, "supported": True,
                               "own": True, "default": False})
    handler.path += f"?session={SID}"
    assert app._get(handler, "/session/thinking-speech")
    assert replies[-1][1]["enabled"] is True


def test_global_default_persists_and_never_replays_existing_thoughts(env):
    thought(env, "Before default enabled")
    assert T.default_setting("t")[1] == {"enabled": False}
    assert T.default_setting("t", body={"enabled": True})[1] == {"enabled": True}
    token = T._DEFAULT.get("token")
    T.default_setting("t", body={"enabled": True})
    assert T._DEFAULT.get("token") == token
    assert T.setting(SID, "t")[1]["own"] is False
    assert T.setting(SID, "t")[1]["enabled"] is True
    settle(env)
    assert not env.calls
    thought(env, "After default enabled")
    settle(env)
    assert [c[-1] for c in env.calls] == ["After default enabled"]
    assert env.watcher.allowed(SID, token, "m")
    T.default_setting("t", body={"enabled": False})
    assert not env.watcher.allowed(SID, token, "m")
    T.default_setting("t", body={"enabled": True})
    assert T._DEFAULT.get("token") != token
    settle(env)
    assert len(env.calls) == 1


def test_thread_overrides_survive_default_changes_and_can_be_cleared(env):
    T.setting(SID, "t", body={"enabled": False})
    T.default_setting("t", body={"enabled": True})
    assert not T.setting(SID, "t")[1]["enabled"]
    assert T.setting(SID, "t")[1]["own"]
    T.setting(SID, "t", body={"enabled": None})
    assert T.setting(SID, "t")[1]["enabled"]
    assert not T.setting(SID, "t")[1]["own"]
    settle(env)
    enable()
    T.default_setting("t", body={"enabled": False})
    assert T.setting(SID, "t")[1]["enabled"]
    thought(env)
    settle(env)
    assert len(env.calls) == 1


def test_global_default_discovers_new_headless_threads_and_skips_unsupported(env, monkeypatch):
    monkeypatch.setattr(sessions, "live_sessions", lambda: {})
    monkeypatch.setattr(sessions, "session_exists", lambda s: s == SID)
    records = []
    monkeypatch.setattr(T.sessiond, "records", lambda: records)
    T.default_setting("t", body={"enabled": True})
    env.watcher.tick()
    assert not env.watcher.cursors
    records.append({"session": SID, "pid": 123})
    settle(env)
    thought(env)
    settle(env)
    assert len(env.calls) == 1
    monkeypatch.setattr(transcript, "transcript_of", lambda s: ("hermes", str(env.path)))
    assert not T.setting(SID, "t")[1]["enabled"]
    env.watcher.tick()
    assert not env.watcher.cursors


def test_default_validation_gate_and_failed_save(env, monkeypatch):
    for body in ({}, {"enabled": None}, {"enabled": 1}, {"enabled": "true"}):
        assert T.default_setting("t", body=body)[1]["status"] == 400
    monkeypatch.setattr(auth, "gate", lambda b: (None, {"status": 403}))
    assert T.default_setting("t")[1]["status"] == 403
    monkeypatch.setattr(auth, "gate", lambda b: ({"id": "u"}, {}))
    def fail(*a):
        raise OSError("No space")
    monkeypatch.setattr(T._DEFAULT, "put", fail)
    assert T.default_setting("t", body={"enabled": True})[1]["status"] == 500


def test_global_http_route(env, monkeypatch):
    import io
    replies = []
    monkeypatch.setattr(app, "_json", lambda h, status, body: replies.append((status, body)))
    body = json.dumps({"enabled": True}).encode()
    handler = SimpleNamespace(path="/speech/thinking", rfile=io.BytesIO(body),
                              headers={"Authorization": "Bearer t", "Content-Length": str(len(body))})
    assert app._post(handler, handler.path)
    assert replies[-1] == (200, {"ok": True, "enabled": True})
    assert app._get(handler, handler.path)
    assert replies[-1] == (200, {"ok": True, "enabled": True})
    assert handler.path in app.CORS_PATHS


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


def test_public_progress_is_read_but_final_and_unlabelled_text_are_not(env):
    enable()
    env.messages[0]["parts"].extend([
        {"type": "reasoning", "redacted": True, "text": ""},
        {"type": "text", "channel": "commentary", "text": "I’m checking the microphone route."},
        {"type": "text", "channel": "final", "text": "The final answer."},
        {"type": "text", "text": "An unlabelled answer."},
    ])
    settle(env)
    assert [c[-1] for c in env.calls] == ["I’m checking the microphone route."]
    settle(env)
    assert len(env.calls) == 1


def test_progress_does_not_read_old_updates_or_replay_after_turn_finishes(env):
    env.messages[0]["parts"].append({"type": "text", "channel": "commentary", "text": "Old progress"})
    enable()
    settle(env)
    assert env.calls == []
    env.messages[0]["parts"].append({"type": "text", "channel": "commentary", "text": "New progress"})
    env.working[0] = False
    settle(env)
    env.working[0] = True
    settle(env)
    assert env.calls == []


@pytest.mark.parametrize("native, pane, expected", [(True, False, True), (False, True, False), (None, True, True)])
def test_codex_activity_uses_native_status_before_a_stale_pane(env, monkeypatch, native, pane, expected):
    from agent_media_server import codex_control
    monkeypatch.setattr(transcript, "transcript_of", lambda s: ("codex", str(env.path)))
    monkeypatch.setattr(codex_control, "working", lambda s: native)
    env.working[0] = pane
    assert T._working(SID) is expected


@pytest.mark.parametrize("status, expected", [({"type": "active", "activeFlags": []}, True),
    ({"type": "idle"}, False), ({"type": "notLoaded"}, None),
    ({"type": "active", "activeFlags": ["waitingOnUserInput"]}, False),
    ({"type": "active", "activeFlags": ["waitingOnApproval"]}, False)])
def test_codex_activity_reads_without_resuming(env, monkeypatch, status, expected):
    from contextlib import contextmanager
    from agent_media_server import codex_control
    calls = []
    class Client:
        def call(self, method, params):
            calls.append((method, params))
            return {"thread": {"status": status}}
    @contextmanager
    def connect(session):
        yield Client()
    monkeypatch.setattr(codex_control, "connect", connect)
    assert codex_control.working(SID) is expected
    assert calls == [("thread/read", {"threadId": SID, "includeTurns": False})]


# --- Claude step names (23 Oct 2026) ----------------------------------------


def step(env, description="Run the server tests", name="Bash", **extra):
    part = {"type": "tool", "name": name, "title": description or name,
            "input_summary": "pytest -q packages/server/tests", "status": "running",
            "result_summary": "", "tool_use_id": f"t{len(env.messages[0]['parts'])}"}
    part.update(extra)
    if description:
        part["description"] = description
    env.messages[0]["parts"].append(part)


@pytest.fixture
def quiet_speech(monkeypatch):
    busy = [False]
    monkeypatch.setattr(T, "_speech_busy", lambda: busy[0])
    return busy


def test_claude_step_description_is_read_but_never_its_command_or_output(env, quiet_speech):
    enable()
    step(env, result_summary="12 passed")
    step(env, None, name="Read", input_summary="/home/x/secret.py")
    settle(env)
    assert [c[-1] for c in env.calls] == ["Run the server tests"]
    said = " ".join(c[-1] for c in env.calls)
    assert "pytest" not in said and "passed" not in said and "secret" not in said


def test_steps_are_latest_only_and_throttled_per_thread(env, quiet_speech):
    enable()
    settle(env)
    step(env, "First step")
    step(env, "Second step")
    step(env, "Third step")
    env.watcher.tick()
    assert [c[-1] for c in env.calls] == ["Third step"]
    env.clock[0] += 5
    step(env, "Too soon")
    env.watcher.tick()
    step(env, "Newer, still too soon")
    env.clock[0] += 5
    env.watcher.tick()
    assert len(env.calls) == 1
    env.clock[0] += T.STEP_GAP_S - 10  # 15 s after the first readout
    env.watcher.tick()
    assert [c[-1] for c in env.calls][-1] == "Newer, still too soon"
    env.watcher.tick()
    assert len(env.calls) == 2
    # A step held past the stale limit is dropped, not read late.
    env.clock[0] += 1
    step(env, "Stale step")
    env.watcher.tick()
    env.clock[0] += T.STEP_STALE_S + 1
    env.watcher.tick()
    assert len(env.calls) == 2


def test_real_thinking_text_is_preferred_over_a_step(env, quiet_speech):
    enable()
    thought(env, "A real thought")
    step(env, "A step")
    settle(env)
    assert [c[-1] for c in env.calls] == ["A real thought"]
    env.clock[0] += 1
    env.watcher.tick()
    assert len(env.calls) == 1


def test_steps_wait_for_speech_and_the_mic(env, quiet_speech):
    enable()
    quiet_speech[0] = True
    step(env, "Held step")
    settle(env)
    assert env.calls == []
    quiet_speech[0] = False
    env.watcher.tick()
    assert [c[-1] for c in env.calls] == ["Held step"]


def test_speech_check_fails_closed(monkeypatch):
    from agent_media_core.intake import submit as S

    def broken():
        raise RuntimeError("no state")
    monkeypatch.setattr(S, "speech_hold_until", broken)
    assert T._speech_busy() is True


@pytest.mark.parametrize("how", ["never", "off", "default-on-thread-off"])
def test_steps_are_never_read_when_thread_thinking_is_off(env, quiet_speech, how):
    if how == "off":
        enable()
        T.setting(SID, "t", body={"enabled": False})
    elif how != "never":
        T.default_setting("t", body={"enabled": True})
        T.setting(SID, "t", body={"enabled": False})
    settle(env)
    step(env)
    settle(env)
    assert env.calls == []


def test_unheard_steps_are_consumed(env, quiet_speech):
    enable()
    speak_priority.set_level(SID, "quiet")
    step(env)
    settle(env)
    speak_priority.set_level(SID, "auto")
    env.watcher.tick()
    assert env.calls == []


def test_other_harnesses_do_not_read_step_descriptions(env, quiet_speech, monkeypatch):
    monkeypatch.setattr(transcript, "transcript_of", lambda s: ("codex", str(env.path)))
    monkeypatch.setattr(T, "_working", lambda s: True)
    enable()
    step(env)
    settle(env)
    assert env.calls == []


def test_claude_transcript_carries_only_plain_words_descriptions(env, quiet_speech, monkeypatch):
    monkeypatch.setattr(transcript, "messages", lambda *a, **kw:
                        transcript.messages_at(str(env.path), harness="claude", **kw))
    enable()
    record = {"type": "assistant", "uuid": "a", "message": {
        "role": "assistant", "stop_reason": "tool_use", "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "rm -rf build", "description": "Clear the  build\nfolder"}},
            {"type": "tool_use", "id": "t2", "name": "mcp__media__calendar_write",
             "input": {"description": "Private event notes"}},
            {"type": "tool_use", "id": "t3", "name": "Agent",
             "input": {"description": "Survey the docs", "prompt": "Long brief"}}]}}
    env.path.write_text(json.dumps(record, separators=(",", ":")) + "\n")
    parsed = transcript.messages(SID, limit=30)[0]
    assert list(T._steps("claude", parsed).values()) == ["Clear the build folder", "Survey the docs"]
    settle(env)
    assert [c[-1] for c in env.calls] == ["Survey the docs"]


def test_a_step_is_spoken_through_submission_with_the_think_earcon(env, monkeypatch):
    """End to end through core's real submission loop, with a fake sink."""
    from pathlib import Path
    from agent_media_core import earcons
    from agent_media_core.intake import submit as S
    from agent_media_core.state import StateStore
    from agent_media_core.types import Target

    played = []

    class Coord:
        def cancel_pre_pause(self): pass
        def pre_pause_remote(self): raise AssertionError("a step pre-paused media")
        def before_speech(self, **kw): pass
        def after_speech(self): pass
        def speaking_line(self, *a): pass

    class Sink:
        def play(self, uri, target, **kw): played.append(uri)
        def play_cue(self, uri, target):
            played.append(("cue", Path(uri).name))
            return True
        def play_playlist(self, uris, target, **kw):
            played.extend(uris)
            return True
        def idle(self, target): return True
        def paused(self, target): return False
        def stop(self, target): pass
        def load_playlist(self, *a): raise AssertionError("loaded a playlist")
        def claim_and_play(self, *a): raise AssertionError("started a playlist")

    def render(text, outfile, **kw):
        Path(outfile).write_bytes(b"fake audio")
        return True, ""

    import time
    # The fixture's frozen clock is the time module's: the earcon waits on it.
    monkeypatch.setattr(T.time, "monotonic", time.perf_counter)
    monkeypatch.setenv("MEDIA_FOLLOW_AUTO", "0")
    for name, value in (("render_text", render), ("_clip_duration", lambda p: 0.0),
                        ("_gate_hold", lambda *a: False), ("_busy_hold", lambda *a: None),
                        ("_ringer_hold", lambda *a: None), ("ringer_quiet", lambda *a: None),
                        ("_remote_playlist", lambda *a: True),
                        ("_source_place", lambda *a: ("", ""))):
        monkeypatch.setattr(S, name, value)
    real = S.submit_event
    events = []

    def submit(event, **kw):
        import dataclasses
        events.append(event)
        event = dataclasses.replace(event, target=Target("local"))
        return real(event, state=StateStore(), sink=Sink(), coordinator=Coord(), **kw)
    monkeypatch.setattr(S, "submit_event", submit)
    enable()
    step(env, "Check the speech route")
    env.watcher.speak(SID, T._PREFS.get(SID), ("m", 0), "Check the speech route")
    assert events[0].metadata["thinking"] is True
    assert events[0].priority.value == "low"
    assert played and played[0] == ("cue", earcons.path("think").name)
    assert len(played) == 2  # the earcon, then the step's one sentence
