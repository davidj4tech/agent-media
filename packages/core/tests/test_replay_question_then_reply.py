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


def _player(monkeypatch, snaps, eof=None):
    """The player reads `snaps` in turn, then stays idle. `eof` is its answer
    to `eof-reached` once idle; None is a desktop mpv's "unavailable"."""
    it = iter(snaps)
    monkeypatch.setattr(cli.ipc, "get_properties",
                        lambda sock, props: next(it, {"idle-active": True}))

    def _get(sock, prop):
        if prop == "eof-reached":
            if eof is None:
                raise cli.ipc.MpvIpcError("property unavailable")
            return eof
        return True                     # idle-active, confirmed

    monkeypatch.setattr(cli.ipc, "get_property", _get)


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


def test_a_stop_through_us_in_the_last_seconds_plays_nothing_more(spawned, monkeypatch):
    """The weak spot the position alone left: a Stop two seconds from the end
    looked heard out. `media stop` / the app's Stop stamp it."""
    from agent_media_core.sinks import speech as sink

    snaps = iter([_playing(1.0), _playing(4.2)])

    def _props(sock, props):
        snap = next(snaps, None)
        if snap is None:
            sink.mark_speech_stopped()
            return {"idle-active": True}
        return snap

    _player(monkeypatch, [])
    monkeypatch.setattr(cli.ipc, "get_properties", _props)
    assert _track() == 0
    assert not spawned


def test_the_phones_player_says_it_was_stopped(spawned, monkeypatch):
    """Its own notification's Stop never reaches us; its eof-reached does."""
    _player(monkeypatch, [_playing(1.0), _playing(4.2)], eof=False)
    assert _track() == 0
    assert not spawned


def test_the_phones_player_says_it_played_out(spawned, monkeypatch):
    # Even when the last poll caught it well short of the end.
    _player(monkeypatch, [_playing(1.0)], eof=True)
    assert _track() == 0
    assert spawned and spawned[-1][-3:] == ["replay", "--id", "42"]


def test_media_stop_notes_what_it_stopped(tmp_path, monkeypatch):
    """The stamp Replay resumes from: the row, the sentence, the reply queued
    behind a question. The lanes' own stops (a newer reply, End of reply) go
    straight to the sink and leave none."""
    from agent_media_core.sinks import speech as sink
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    StateStore().set_now_playing("speech", uri="/q.mp3", started_at=5.0,
                                 target="phone",
                                 extras={"history_id": 7, "then_id": 8,
                                         "current_sentence_idx": 0,
                                         "source_session": "aaa"})
    monkeypatch.setattr(cli, "_active_speech_target", lambda: None)
    monkeypatch.setattr(cli.SinkSpeech, "stop", lambda self, t=None: None)
    assert cli.cmd_stop(argparse.Namespace()) == 0
    got = sink.last_stop()
    assert (got["history_id"], got["then_id"], got["sentence"]) == (7, 8, 0)
