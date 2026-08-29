"""What is being timed is the reply, not the sentence it is up to.

A reply is rendered one clip per sentence and queued as a playlist, so the
player's `duration` is whichever sentence it is reading and its `time-pos`
starts again at every full stop. The local lane has always lifted that onto
the response's timeline; the phone lane — the one every reply takes by
default — returned the player's reading raw, so the popup's bar restarted
mid-reply and its end time was the sentence's, while the tmux bar beside it
(which reads the announced timeline) said the reply's.
"""

from __future__ import annotations

import pytest

from agent_media_core import cli


TOTAL = 11.04
CLIPS = [4.368, 2.28, 4.392]


@pytest.fixture
def row(monkeypatch):
    """A reply of three sentences, in flight."""
    ex = {"total_duration_s": TOTAL, "clip_durations_s": list(CLIPS)}
    monkeypatch.setattr(cli, "_now_speaking", lambda: {"extras": ex})
    return ex


@pytest.fixture
def remote(monkeypatch):
    monkeypatch.setattr(cli, "_remote_speech", lambda: True)
    monkeypatch.setattr(cli, "_sock", lambda: "tcp://phone.example:6612")
    cli._SNAP_CACHE["value"] = None


def _snap(monkeypatch, **props):
    base = {"idle-active": False, "pause": False, "mute": False, "speed": 1.0}
    base.update(props)
    monkeypatch.setattr(cli, "_remote_snapshot", lambda: base)


def test_the_end_is_the_reply_not_the_sentence(row, remote, monkeypatch):
    _snap(monkeypatch, **{"time-pos": 1.0, "duration": CLIPS[0],
                          "playlist-pos": 0})
    _idle, pos, dur, *_ = cli._speech_display_state()
    assert dur == TOTAL, "the end time was the sentence being read"
    assert pos == 1.0


def test_the_bar_does_not_restart_at_a_full_stop(row, remote, monkeypatch):
    """Two seconds into the third sentence is nine seconds into the reply."""
    _snap(monkeypatch, **{"time-pos": 2.0, "duration": CLIPS[2],
                          "playlist-pos": 2})
    _idle, pos, dur, *_ = cli._speech_display_state()
    assert pos == pytest.approx(CLIPS[0] + CLIPS[1] + 2.0)
    assert dur == TOTAL


def test_both_lanes_agree(row, monkeypatch):
    """The popup takes the snapshot and the status bar the announced timeline.
    They are looking at one reply and must not answer differently."""
    _snap(monkeypatch, **{"time-pos": 2.0, "duration": CLIPS[2],
                          "playlist-pos": 2})
    monkeypatch.setattr(cli, "_remote_speech", lambda: True)
    monkeypatch.setattr(cli, "_sock", lambda: "tcp://phone.example:6612")
    cli._SNAP_CACHE["value"] = None
    _i, _pos, snapshot_dur, *_ = cli._speech_display_state()

    row["play_started_at"] = 0.0
    row["live_pos_s"] = 2.0
    row["writer_pid"] = None
    _i, _pos, announced_dur, *_ = cli._speech_display_state(prefer_local=True)
    assert snapshot_dur == announced_dur == TOTAL


def test_a_lane_that_announced_nothing_keeps_the_player_reading(
        remote, monkeypatch):
    """The remote-render lane records no timeline. An end time invented there
    would be worse than the sentence's honest one."""
    monkeypatch.setattr(cli, "_now_speaking", lambda: {"extras": {}})
    _snap(monkeypatch, **{"time-pos": 1.0, "duration": 4.0,
                          "playlist-pos": 0})
    _idle, pos, dur, *_ = cli._speech_display_state()
    assert (pos, dur) == (1.0, 4.0)


def test_a_player_that_reports_no_position_still_places_the_reply(
        row, remote, monkeypatch):
    """Between two sentences there is a moment with no clip loaded; the reply
    has still got as far as the sentences already read."""
    _snap(monkeypatch, **{"time-pos": None, "duration": None,
                          "playlist-pos": 1})
    _idle, pos, dur, *_ = cli._speech_display_state()
    assert pos == pytest.approx(CLIPS[0])
    assert dur == TOTAL
