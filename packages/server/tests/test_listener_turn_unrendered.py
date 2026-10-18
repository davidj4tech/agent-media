"""A listener turn with no audio still makes the thread pending and joins its
user message (David, 18 Oct 2026: the listener's words are a row, not an mp3).
Fakes only; the state dir is the test's own and nothing is rendered or played.
"""

from __future__ import annotations

import os

import pytest

from agent_media_core import book_tracks
from agent_media_server import threads
from agent_media_server import transcript as T

S = "6c73498c-02c1-4846-8350-a82006973571"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith(("MEDIA_", "RELAY_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    import agent_media_core.render.engines as eng

    def no_render(*a, **k):
        raise AssertionError("a listener turn is never rendered")

    monkeypatch.setattr(eng, "render_text", no_render)
    monkeypatch.setattr(book_tracks, "_live_turn", lambda s: None)
    return tmp_path


def _lines(tmp_path):
    assert book_tracks.record_listener_turn(S, "Can you check the build?") is True
    return book_tracks.conversation_log(S, tmp_path / "nofolder", positions=False)


def test_pending_while_the_last_line_is_an_unrendered_turn(isolated, monkeypatch):
    lines = _lines(isolated)
    assert [l["who"] for l in lines] == ["you"]
    monkeypatch.setattr(threads, "attach_pictures", lambda lines: None)
    monkeypatch.setattr(threads.sessions, "live_sessions", lambda: {})
    monkeypatch.setattr(threads.driver, "headless_state", lambda s: None)
    monkeypatch.setattr(threads.sessions, "approval_for", lambda *a, **k: None)
    monkeypatch.setattr(threads.sessions, "suggestion_for", lambda *a, **k: "")
    monkeypatch.setattr(threads, "messages_for", lambda *a, **k: ([], False))
    monkeypatch.setattr(threads.recaps, "recap_for", lambda s: None)
    monkeypatch.setattr(threads.context, "context_for", lambda s: None)
    from agent_media_core import activity
    monkeypatch.setattr(activity, "attach", lambda s, lines: None)
    assert threads._envelope(S, lines)["pending"] is True


def test_an_unrendered_turn_joins_its_user_message(isolated):
    lines = _lines(isolated)
    you = lines[0]
    msgs = [{"role": "user", "at": you["at"] + 2, "spoken": None,
             "parts": [{"type": "text", "text": "Can you check the build?"}]}]
    T.join_speech(msgs, lines)
    assert msgs[0]["spoken"]["id"] == you["id"]
