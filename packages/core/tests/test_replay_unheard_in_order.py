"""Playing an unheard reply goes on through the thread's unheard ones after it.

With several replies waiting unheard in a thread, opening it (or pressing the
oldest one's Play) reads every one of them in order, not just the newest
(David, 16 Oct 2026). Each replay hands the next unheard row to its follower
as `--then-id` (tested in test_replay_question_then_reply.py), so a Stop ends
the run.
"""

from __future__ import annotations

import argparse
import time

import pytest

from agent_media_core import cli
from agent_media_core.state import StateStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_row_has_audio", lambda r: True)
    return StateStore()


def _row(st, text, session="s1", **ex):
    return st.add_history(sink="speech", uri="/x.mp3", started_at=time.time(),
                          target="phone", source="claude-code", text=text,
                          extras={"source_session": session, **ex})


def test_the_next_unheard_is_the_oldest_after_it_in_the_same_thread(store):
    first = _row(store, "one", held=True)
    _row(store, "elsewhere", session="s2", held=True)
    heard = _row(store, "heard", held=True)
    store.mark_heard(heard)
    _row(store, "spoken at once")                       # not held
    second = _row(store, "two", held=True)
    third = _row(store, "three", held=True)

    def next_of(rid):
        return cli._next_unheard(store.history_row(rid))

    assert next_of(first) == second
    assert next_of(second) == third
    assert next_of(third) is None


def test_only_an_unheard_reply_goes_on(store):
    played = _row(store, "played", held=True)
    store.mark_heard(played)
    plain = _row(store, "plain")
    _row(store, "waiting", held=True)
    assert cli._next_unheard(store.history_row(played)) is None
    assert cli._next_unheard(store.history_row(plain)) is None


def test_replay_by_id_chains_the_next_unheard(store, monkeypatch):
    first = _row(store, "one", held=True)
    second = _row(store, "two", held=True)
    calls = []
    monkeypatch.setattr(cli, "_replay_row",
                        lambda row, **kw: calls.append((row["id"], kw)) or 0)
    a = argparse.Namespace(id=first, from_sentence=None, again=False, lock_fd=7)
    assert cli.cmd_replay(a) == 0
    # The token handed down by the reply before it goes on to its replay.
    assert calls == [(first, {"then_id": second, "lock_fd": 7})]
