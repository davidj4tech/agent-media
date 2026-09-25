"""Always speak (speak_priority.py): the flag, and that it lifts a pane mute
but never a held reply."""

from __future__ import annotations

import json

from agent_media_core import speak_priority
from agent_media_core.intake.submit import _unmuted_by_priority
from agent_media_core.types import Event, Priority, Source

SID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _event(**meta) -> Event:
    return Event(text="hi", source=Source.CLAUDE_CODE, priority=Priority.NORMAL,
                 metadata=meta)


def test_set_and_clear_writes_only_on_change(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert not speak_priority.is_priority(SID)
    assert speak_priority.set_priority(SID, True) is True
    assert speak_priority.set_priority(SID, True) is False
    assert speak_priority.is_priority(SID)
    assert json.loads((speak_priority._path()).read_text())[SID]["level"] == "auto"
    assert speak_priority.set_priority(SID, False) is True
    assert speak_priority.priority_sessions() == {}
    assert not speak_priority.is_priority("")


def test_priority_lifts_a_mute_but_not_a_hold(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert _unmuted_by_priority(True, _event(), SID) is True       # plain mute
    speak_priority.set_priority(SID, True)
    assert _unmuted_by_priority(True, _event(), SID) is False      # lifted
    assert _unmuted_by_priority(True, _event(held=True), SID) is True
    assert _unmuted_by_priority(True, _event(), "other") is True
    assert _unmuted_by_priority(False, _event(), SID) is False


def test_a_priority_conversation_is_never_held_by_the_toast(tmp_path, monkeypatch):
    from agent_media_core.intake import hook_claude_code as hook
    from agent_media_core.intake import toast

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    played, held = [], []
    monkeypatch.setattr(hook, "_play_detached", played.append)
    monkeypatch.setattr(toast, "should_hold", lambda session="": True)
    monkeypatch.setattr(toast, "hold", held.append)
    payload = {"session_id": SID, "last_assistant_message": "Done."}
    hook._handle_stop(payload)
    assert len(held) == 1 and played == []
    speak_priority.set_priority(SID, True)
    hook._handle_stop(payload)
    assert len(held) == 1 and len(played) == 1
    # Quiet gets no toast: archived unheard, straight to the (held) render.
    speak_priority.set_level(SID, "quiet")
    hook._handle_stop({**payload, "last_assistant_message": "Quietly done."})
    assert len(held) == 1 and len(played) == 2 and played[-1].metadata["held"]


def test_levels_and_the_older_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    speak_priority._path().parent.mkdir(parents=True)
    speak_priority._path().write_text(json.dumps({SID: 1.0}))     # the old "always speak"
    assert speak_priority.level_of(SID) == "auto" and speak_priority.is_priority(SID)
    assert speak_priority.set_level(SID, "quiet") is True
    assert speak_priority.level_of(SID) == "quiet" and not speak_priority.is_priority(SID)
    assert speak_priority.levels() == {SID: "quiet"}
    assert speak_priority.priority_sessions() == {}
    speak_priority.set_level(SID, "normal")
    assert speak_priority.levels() == {}
    import pytest
    with pytest.raises(ValueError):
        speak_priority.set_level(SID, "loud")


def test_interrupt_is_high_and_quiet_is_held(tmp_path, monkeypatch):
    from agent_media_core.intake import hook_claude_code as hook
    from agent_media_core.intake import toast

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    played, held = [], []
    monkeypatch.setattr(hook, "_play_detached", played.append)
    monkeypatch.setattr(toast, "should_hold", lambda session="": True)
    monkeypatch.setattr(toast, "hold", held.append)
    payload = {"session_id": SID, "last_assistant_message": "Done."}
    speak_priority.set_level(SID, "interrupt")
    hook._handle_stop(payload)
    assert held == [] and played[-1].priority == Priority.HIGH
    speak_priority.set_level(SID, "quiet")
    hook._handle_stop(payload)
    assert held == [] and played[-1].metadata.get("held") is True       # no toast either
    assert played[-1].priority == Priority.NORMAL


def test_a_question_follows_the_level(tmp_path, monkeypatch):
    """Normal holds a question nobody is looking at, and so does quiet (it
    needs an answer); auto speaks it at once. Answering drops the toast."""
    from agent_media_core.intake import hook_claude_code as hook
    from agent_media_core.intake import toast

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(hook, "submit_event", lambda e, state=None: sent.append(e))
    monkeypatch.setattr(hook, "_claim_once", lambda ask: True)
    monkeypatch.setattr(hook, "_dedup_seen", lambda state, msg: False)
    monkeypatch.setattr(hook, "_ask_location_label", lambda: "")
    monkeypatch.setattr(hook, "_client_pane_focused", lambda: False)
    monkeypatch.setattr(toast, "should_hold", lambda session="": True)
    monkeypatch.setattr(toast, "show", lambda where: None)
    monkeypatch.setattr(toast, "_where", lambda pane: "")
    payload = {"session_id": SID}

    hook._emit_ask("Which one?", payload)
    assert sent[-1].metadata.get("held") and len(toast.listing()) == 1
    speak_priority.set_level(SID, "quiet")
    hook._emit_ask("Which two?", payload)
    assert sent[-1].metadata.get("held") and len(toast.listing()) == 2
    speak_priority.set_level(SID, "auto")
    hook._emit_ask("Which three?", payload)
    assert not sent[-1].metadata.get("held") and len(toast.listing()) == 2

    monkeypatch.setattr(toast, "_row_for", lambda key: None)
    toast.drop_asks(SID)
    assert toast.listing() == []


def test_default_level_covers_every_conversation_without_its_own(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert speak_priority.default_level() == "normal"
    speak_priority.set_level(SID, "auto")
    assert speak_priority.set_default("quiet") is True
    assert speak_priority.set_default("quiet") is False
    assert speak_priority.level_of("other") == "quiet"
    assert speak_priority.level_of(SID) == "auto"
    assert speak_priority.set_level(SID, "quiet") is True        # back to the default
    assert speak_priority.levels() == {}
    assert speak_priority.set_level(SID, "quiet") is False
    assert speak_priority.set_level(SID, "normal") is True       # normal is its own now
    assert speak_priority.levels() == {SID: "normal"}
    speak_priority.set_default("normal")
    assert speak_priority.level_of("other") == "normal"

