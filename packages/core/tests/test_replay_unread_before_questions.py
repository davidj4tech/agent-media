"""A question waits while a thread's unread replies are read.

David, 18 Oct 2026: "when text is being read from unread chats, a user
question should wait until they're finished. otherwise the questions just
sent above the follow along and it can't be followed." A replay of an unread
reply holds the token at `_UNREAD_RANK` (above HIGH, below URGENT), and the
run hands the token from one reply's follower to the next replay instead of
releasing it, so a waiting question gets in neither during a reply nor
between two. Fakes only: no player, no phone; the state dir is the test's own.
"""

from __future__ import annotations

import argparse
import os
import time

import pytest

from agent_media_core import cli
from agent_media_core.intake import submit as S
from agent_media_core.state import StateStore
from agent_media_core.types import Priority


@pytest.fixture(autouse=True)
def state_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def _waiter(rank: int, session: str) -> S._SpeechPlaybackLock:
    w = S._SpeechPlaybackLock()
    w._rank, w._session, w._seq = rank, session, time.time()
    w._register()
    return w


def _token(rank: int) -> S._SpeechPlaybackLock:
    lock = S._SpeechPlaybackLock(kind="replay")
    assert lock.take_within(1.0, rank=rank, session="replay:test")
    return lock


def test_an_unread_reply_is_ranked_above_a_question():
    assert cli._replay_rank({"extras": {"held": True}}) == S._UNREAD_RANK
    assert cli._replay_rank({"extras": {"held": True, "heard": True}}) == S._REPLAY_RANK
    assert cli._replay_rank({"extras": {}}) == S._REPLAY_RANK
    assert S._PRIO_RANK[Priority.HIGH] < S._UNREAD_RANK < S._PRIO_RANK[Priority.URGENT]


def test_a_question_waits_for_an_unread_reply_but_urgent_does_not():
    token = _token(S._UNREAD_RANK)
    try:
        high = _waiter(S._PRIO_RANK[Priority.HIGH], "%3")
        assert not token.should_yield()
        high._unregister()
        urgent = _waiter(S._PRIO_RANK[Priority.URGENT], "%1")
        assert token.should_yield()
        urgent._unregister()
    finally:
        token.release()


def test_the_run_hands_the_token_to_the_next_reply(monkeypatch):
    """The follower starts the next unread reply with the token passed down
    (`--lock-fd`), and the token is never free in between."""
    StateStore().set_now_playing("speech", uri="/q.mp3", started_at=time.time(),
                                 target="phone", extras={})
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "_sock", lambda: "/s")
    it = iter([{"idle-active": False, "playlist-pos": 0, "time-pos": t,
                "pause": False, "speed": 1.0, "mute": False} for t in (1.0, 4.2)])
    monkeypatch.setattr(cli.ipc, "get_properties",
                        lambda sock, props: next(it, {"idle-active": True}))
    monkeypatch.setattr(cli.ipc, "get_property", lambda sock, prop: True)
    child: list = []

    def popen(argv, **kw):
        # The child inherits the descriptor: keep a copy, as it would.
        fds = kw.get("pass_fds") or ()
        child.append((argv, [os.dup(fd) for fd in fds]))

    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    token = _token(S._UNREAD_RANK)
    fd = token.fileno
    assert cli.cmd_replay_track(argparse.Namespace(
        sentences="", offsets="", pane="", durations="[5.0]", lock_fd=fd,
        rank=S._UNREAD_RANK, then_id=42)) == 0
    argv, kept = child[-1]
    assert argv[-5:] == ["replay", "--id", "42", "--lock-fd", str(fd)]
    assert len(kept) == 1
    question = S._SpeechPlaybackLock()
    assert not question.take_within(0.3, rank=S._PRIO_RANK[Priority.HIGH],
                                    session="%3"), "free between two unread replies"
    os.close(kept[0])               # the run is over
    assert question.take_within(1.0, rank=S._PRIO_RANK[Priority.HIGH], session="%3")
    question.release()
