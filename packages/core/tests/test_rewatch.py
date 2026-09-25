"""A Normal reply let through because its thread was on screen is asked
again when its turn to speak comes: it may have waited behind another
thread's speech while the listener moved on."""

from __future__ import annotations

import time

from agent_media_core import watching
from agent_media_core.intake import submit, toast
from agent_media_core.types import Event, Priority, Source


def _event(watched_ago=60.0, **md):
    return Event(text="the answer", source=Source.CLAUDE_CODE,
                 priority=Priority.NORMAL, voice="af_heart",
                 metadata={"kind": "stop", "session": "s1",
                           "watched_at": time.time() - watched_ago, **md})


def _remembered(monkeypatch):
    got = []
    monkeypatch.setattr(toast, "remember", lambda event, *a, **k: got.append(event))
    return got


def test_left_the_thread_while_it_waited_holds_it(monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)      # a headless session
    got = _remembered(monkeypatch)
    watching.publish({"s2": 1})                         # on another thread
    ev = _event()
    assert submit._unwatched_by_now(ev, "s1")
    assert ev.metadata["held"] is True and got == [ev]


def test_still_on_the_thread_plays(monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    got = _remembered(monkeypatch)
    watching.publish({"s1": 1})
    ev = _event()
    assert not submit._unwatched_by_now(ev, "s1")
    assert "held" not in ev.metadata and not got


def test_only_a_reply_the_hook_let_through_and_not_just_now(monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    got = _remembered(monkeypatch)
    watching.publish({})
    # No `watched_at`: an auto/interrupt reply, a `media say`, a question.
    plain = _event()
    plain.metadata.pop("watched_at")
    assert not submit._unwatched_by_now(plain, "s1")
    # Decided a moment ago: no second opinion needed.
    assert not submit._unwatched_by_now(_event(watched_ago=1.0), "s1")
    assert not got


def test_a_level_changed_to_auto_meanwhile_plays(monkeypatch):
    from agent_media_core import speak_priority

    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setattr(speak_priority, "level_of", lambda s: "auto")
    got = _remembered(monkeypatch)
    watching.publish({})
    assert not submit._unwatched_by_now(_event(), "s1")
    assert not got


def test_a_failed_check_plays_it(monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    watching.publish({})

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(toast, "remember", boom)
    ev = _event()
    assert not submit._unwatched_by_now(ev, "s1")
    assert "held" not in ev.metadata
