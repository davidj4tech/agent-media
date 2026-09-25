"""A replayed question goes on to its answer — but only if it was heard out.

The bar's Replay reads the listener's question and then the reply to it
(David, 2026-09-25). The question's follower starts the reply once the player
goes idle at the end of the last clip; a Stop mid-question leaves the player
just as idle, so the last position seen is what tells them apart.
"""

from __future__ import annotations

import argparse
import time

import pytest

from agent_media_core import cli
from agent_media_core.state import StateStore


@pytest.fixture
def spawned(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    StateStore().set_now_playing("speech", uri="/q.mp3", started_at=time.time(),
                                 target="phone", extras={})
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "_sock", lambda: "/s")
    calls: list = []
    monkeypatch.setattr(cli.subprocess, "Popen",
                        lambda argv, **kw: calls.append(argv))
    return calls


def _player(monkeypatch, snaps):
    """The player reads `snaps` in turn, then stays idle."""
    it = iter(snaps)
    monkeypatch.setattr(cli.ipc, "get_properties",
                        lambda sock, props: next(it, {"idle-active": True}))
    # Asked only for idle-active: the confirming read after an idle snapshot.
    monkeypatch.setattr(cli.ipc, "get_property", lambda sock, prop: True)


def _track():
    return cli.cmd_replay_track(argparse.Namespace(
        sentences="", offsets="", pane="", durations="[5.0]", lock_fd=-1,
        then_id=42))


def _playing(tp):
    return {"idle-active": False, "playlist-pos": 0, "time-pos": tp,
            "pause": False, "speed": 1.0, "mute": False}


def test_heard_out_the_question_hands_on_to_its_reply(spawned, monkeypatch):
    _player(monkeypatch, [_playing(1.0), _playing(4.2)])
    assert _track() == 0
    assert spawned and spawned[-1][-3:] == ["replay", "--id", "42"]


def test_a_stop_mid_question_plays_nothing_more(spawned, monkeypatch):
    _player(monkeypatch, [_playing(0.5), _playing(1.2)])
    assert _track() == 0
    assert not spawned
