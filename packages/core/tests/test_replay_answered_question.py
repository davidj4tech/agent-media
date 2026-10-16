"""A question read back stops when it is answered, as one read live does.

A thread's open plays its held questions (732b27b), so most questions are
heard as a replay — and the replay only knew the `read` cut, so it read on
after the answer (David, 2026-10-16: 23 of the day's 40 questions).
"""

from __future__ import annotations

import argparse
import time

import pytest

from agent_media_core import cli
from agent_media_core.intake import submit
from agent_media_core.state import StateStore

SESSION = "sess-q"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "_sock", lambda: "/s")
    monkeypatch.setattr(cli.subprocess, "Popen", lambda argv, **kw: None)
    sent: list = []
    monkeypatch.setattr(cli.ipc, "command",
                        lambda sock, *args, **kw: sent.append(args))
    monkeypatch.setattr(cli.ipc, "get_property", lambda sock, prop: True)
    heard: list = []
    monkeypatch.setattr(cli, "_mark_heard", heard.append)
    return sent, heard


def _now_playing(ask: bool):
    extras = {"source_session": SESSION}
    if ask:
        extras["ask"] = [{"question": "Install now?", "options": []}]
    StateStore().set_now_playing("speech", uri="/q.mp3", started_at=time.time(),
                                 target="phone", extras=extras)


def _player(monkeypatch, answer_at_tick: int, at=None):
    """Plays mid-question; the answer lands on tick `answer_at_tick`."""
    ticks = [0]

    def _props(sock, props):
        ticks[0] += 1
        if ticks[0] == answer_at_tick:
            submit.request_session_speech_cut(SESSION, "ask", at)
        if ticks[0] > 20:
            return {"idle-active": True}
        return {"idle-active": False, "playlist-pos": 0, "time-pos": 1.0,
                "pause": False, "speed": 1.0, "mute": False}

    monkeypatch.setattr(cli.ipc, "get_properties", _props)
    return ticks


def _track():
    return cli.cmd_replay_track(argparse.Namespace(
        sentences="", offsets="", pane="", durations="[30.0]", lock_fd=-1,
        then_id=None, heard_id=9))


def test_an_answer_stops_the_question_and_it_is_heard(rig, monkeypatch):
    sent, heard = rig
    _now_playing(ask=True)
    ticks = _player(monkeypatch, answer_at_tick=3)
    assert _track() == 0
    assert ticks[0] < 10
    assert ("stop",) in sent
    assert heard == [9]


def test_a_reply_that_is_not_a_question_reads_on(rig, monkeypatch):
    sent, heard = rig
    _now_playing(ask=False)
    ticks = _player(monkeypatch, answer_at_tick=3)
    _track()
    assert ticks[0] > 20


def test_an_answer_from_before_the_replay_is_another_question(rig, monkeypatch):
    sent, heard = rig
    _now_playing(ask=True)
    ticks = _player(monkeypatch, answer_at_tick=3, at=time.time() - 60)
    _track()
    assert ticks[0] > 20
