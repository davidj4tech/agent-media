"""A thread's question card waits while its unread replies are read.

David, 18 Oct 2026: "The card should wait too. otherwise I can't see the
follow along text". The voice of a question already waits (core
`_UNREAD_RANK`); the card, docked above the composer, took the room the
follow-along was in. While the speech row says an unread reply of this
thread is being read (`extras.unread`, written by `media replay`), the log
carries no `approval` and no unanswered ask part. Fakes only; the state dir
is the test's own.
"""

from __future__ import annotations

import time

import pytest

from agent_media_core.state import StateStore
from agent_media_server import threads

S = "6c73498c-02c1-4846-8350-a82006973571"
CARD = {"kind": "question", "key": "k", "questions": []}


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    threads._unread_seen.clear()
    yield StateStore()
    threads._unread_seen.clear()


def _reading(st, session=S, **ex):
    st.set_now_playing("speech", uri="/c.mp3", started_at=time.time(), target="phone",
                       extras={"replay": True, "unread": True,
                               "source_session": session, **ex})


def test_the_card_waits_while_this_thread_is_read(state):
    _reading(state)
    assert threads.reading_unread(S)
    assert not threads.reading_unread("other")


def test_a_held_question_being_read_is_not_kept_back(state):
    _reading(state, ask=[{"question": "Which?"}])
    assert not threads.reading_unread(S)


def test_it_stays_back_across_the_hand_on_then_comes(state):
    _reading(state)
    assert threads.reading_unread(S, now=100.0)
    state.clear_now_playing("speech")
    assert threads.reading_unread(S, now=100.0 + threads.UNREAD_GRACE_S - 1)
    assert not threads.reading_unread(S, now=100.0 + threads.UNREAD_GRACE_S + 1)


def test_an_ordinary_replay_does_not_hold_it(state):
    state.set_now_playing("speech", uri="/c.mp3", started_at=time.time(), target="phone",
                          extras={"replay": True, "source_session": S})
    assert not threads.reading_unread(S)


def _envelope(monkeypatch):
    msgs = [{"id": "a", "role": "assistant", "parts": [
                {"type": "text", "text": "One."},
                {"type": "ask", "ask": [], "status": "running", "answer": ""}]},
            {"id": "b", "role": "assistant", "parts": [
                {"type": "ask", "ask": [], "status": "running", "answer": ""}]},
            {"id": "c", "role": "assistant", "parts": [
                {"type": "ask", "ask": [], "status": "done", "answer": "Yes"}]}]
    monkeypatch.setattr(threads, "attach_pictures", lambda lines: None)
    monkeypatch.setattr(threads.sessions, "live_sessions", lambda: {S: "%7"})
    monkeypatch.setattr(threads.sessions, "_agent_of_pane", lambda pane: "claude")
    monkeypatch.setattr(threads.sessions, "approval_for", lambda *a, **k: dict(CARD))
    monkeypatch.setattr(threads.sessions, "suggestion_for", lambda *a, **k: "")
    monkeypatch.setattr(threads, "messages_for",
                        lambda *a, **k: ([dict(m, parts=list(m["parts"])) for m in msgs], False))
    monkeypatch.setattr(threads.recaps, "recap_for", lambda s: None)
    monkeypatch.setattr(threads.context, "context_for", lambda s: None)
    from agent_media_core import activity
    monkeypatch.setattr(activity, "attach", lambda s, lines: None)
    return threads._envelope(S, [])


def test_the_log_has_no_card_while_reading(state, monkeypatch):
    _reading(state)
    out = _envelope(monkeypatch)
    assert out["approval"] is None
    assert [m["id"] for m in out["messages"]] == ["a", "c"]
    assert [p["type"] for p in out["messages"][0]["parts"]] == ["text"]


def test_the_log_has_the_card_otherwise(state, monkeypatch):
    out = _envelope(monkeypatch)
    assert out["approval"] == CARD
    assert len(out["messages"]) == 3
