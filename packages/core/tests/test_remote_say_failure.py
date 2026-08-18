"""A remote render that never spoke must not be recorded as speech.

Speech over the phone lane is write-only: the text is piped to a script on
another device and nothing comes back but an exit status. When that status
went unread, a command pointing at a script the far side had deleted produced
exactly one symptom — silence — while history recorded every reply as spoken.
The agent believed it had spoken, `media history` agreed, and nothing anywhere
disagreed with the room. It went unnoticed for an hour.

So: a non-zero exit is an error in the error log, and the history row says so.
"""

import json
import os

import pytest

from agent_media_core.intake import submit
from agent_media_core.state import StateStore
from agent_media_core.types import Event, Priority, Source


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return StateStore()


def _event():
    return Event(text="a reply nobody heard", source=Source.CLAUDE_CODE,
                 priority=Priority.NORMAL, metadata={"session": "s1"})


def _run(store, monkeypatch, cmd):
    monkeypatch.setattr(submit, "_wait_speech_hold", lambda *a, **k: None)
    monkeypatch.setattr(submit, "_speech_flushed", lambda *a, **k: False)

    class _Coord:
        def pre_pause_remote(self): pass
        def before_speech(self, title="", priority="", defer_music=False,
                      text=""): pass
        # Deferred and applied when the far side says it is about to play.
        def duck_music_now(self): pass
        def after_speech(self): pass

    return submit._submit_remote_say(
        "a reply nobody heard", cmd, _Coord(), store, _event())


def _extras(store) -> dict:
    row = store.recent_history(sink="speech", limit=1)[0]
    ex = row.get("extras") or {}
    return ex if isinstance(ex, dict) else json.loads(ex or "{}")


def test_failed_remote_render_is_marked_and_logged(store, monkeypatch):
    _run(store, monkeypatch, "exit 3")

    extras = _extras(store)
    assert extras.get("failed"), (
        "history recorded an unspoken reply as spoken — the failure mode that "
        "hid a dead renderer path for an hour")
    assert "3" in str(extras["failed"])

    errs = store.recent_errors(limit=10)
    assert any("remote-say" in str(e) for e in errs), \
        "a reply that was never spoken left nothing in `media errors`"


def test_successful_remote_render_is_not_marked(store, monkeypatch):
    _run(store, monkeypatch, "cat >/dev/null")

    assert "failed" not in _extras(store)
