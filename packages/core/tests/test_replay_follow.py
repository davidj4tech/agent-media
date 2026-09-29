"""Following along on a replay, for a reply that is one clip.

The phone lane renders a whole reply into a single file, so a replay of it has
no playlist position to read the sentence off — and the player is 400ms away
behind a circuit breaker, so it cannot be asked either. The first slow read
trips the breaker for 45s, the next few are refused outright, and a tracker
that polls concludes playback has ended: two seconds into a reply that is
audibly still going, the row is cleared and the follow-along is over.

The timeline recorded when the reply first played is the answer, read against
the clock — the same trade the live lane already makes.
"""

from __future__ import annotations

import argparse
import time

import pytest

from agent_media_core import cli
from agent_media_core.state import StateStore


SENTS = ["One replayed sentence.", "Two replayed sentence.",
         "Three replayed sentence."]
OFFS = [0.0, 0.15, 0.3]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    s = StateStore()
    s.set_now_playing("speech", uri="remote-say:phone", started_at=time.time(),
                      target="phone",
                      extras={"clip_sentences": SENTS, "clip_offsets_s": OFFS})
    return s


@pytest.fixture(autouse=True)
def quiet_player(monkeypatch):
    """No test asks a real player (the shell's config names the phone); one
    that wants an answer sets its own."""
    monkeypatch.setattr(cli, "_player_says_idle", lambda target: None)


_READ_IDLE = cli._player_says_idle


def _track(**kw):
    return cli.cmd_replay_track(argparse.Namespace(
        **{"sentences": cli.json.dumps(SENTS), "offsets": cli.json.dumps(OFFS),
           "pane": "", "durations": cli.json.dumps([0.45]), **kw}))


def test_the_clock_carries_the_replay(store, monkeypatch):
    seen: list = []
    orig = store.set_now_playing

    def _spy(sink, **kw):
        ex = kw.get("extras") or {}
        if "current_sentence_idx" in ex:
            seen.append(ex["current_sentence_idx"])
        return orig(sink, **kw)

    monkeypatch.setattr(StateStore, "set_now_playing",
                        lambda self, sink, **kw: _spy(sink, **kw))
    _track()
    assert seen == [0, 1, 2], f"sentences did not step in order: {seen}"


def test_a_refused_player_does_not_end_the_replay(store, monkeypatch):
    """The breaker refusing every read is the normal state of this lane, not a
    reason to decide the audio stopped."""
    def _boom(*a, **kw):
        raise RuntimeError("skipped, endpoint slow (43s left)")

    monkeypatch.setattr(cli.ipc, "get_properties", _boom)
    monkeypatch.setattr(cli.ipc, "get_property", _boom)
    started = time.time()
    _track()
    assert time.time() - started > 0.4, "the replay was cut short"


def test_the_row_is_cleared_when_the_timeline_runs_out(store):
    _track()
    assert store.get_now_playing("speech") is None


def test_it_writes_what_it_knows_and_not_what_it_does_not(store):
    """No pause/speed/mute: only the player knows those, and inventing them
    would be worse than letting the display fall back."""
    cli._mirror_clock(store, lambda ex: True, SENTS, OFFS, 1, 0.2)
    ex = (store.get_now_playing("speech") or {}).get("extras") or {}
    assert ex["current_sentence"] == SENTS[1]
    assert ex["current_sentence_idx"] == 1
    assert ex["live_pos_s"] == 0.2
    assert "live_pause" not in ex and "live_speed" not in ex


def test_a_row_taken_over_is_left_alone(store):
    cli._mirror_clock(store, lambda ex: False, SENTS, OFFS, 2, 0.4)
    ex = (store.get_now_playing("speech") or {}).get("extras") or {}
    assert "current_sentence" not in ex


def _slow_track(monkeypatch, answers):
    """A replay long enough for the player to be asked, fast."""
    said = iter(answers)
    monkeypatch.setattr(cli, "_REPLAY_ALIVE_EVERY_S", 0.05)
    monkeypatch.setattr(cli, "_player_says_idle",
                        lambda target: next(said, answers[-1]))
    started = time.time()
    cli.cmd_replay_track(argparse.Namespace(
        sentences=cli.json.dumps(SENTS), offsets=cli.json.dumps([0.0, 0.1, 30.0]),
        pane="", durations=cli.json.dumps([60.0])))
    return time.time() - started


def test_a_player_that_went_away_ends_the_replay(store, monkeypatch):
    """An install restarts the app, and its player comes back empty. The clock
    alone read on in silence to the end of the reply (David, 29 Sep 2026)."""
    stops: list = []
    monkeypatch.setattr("agent_media_core.sinks.speech.mark_speech_stopped",
                        lambda sentence=None, tick=True: stops.append(sentence))
    took = _slow_track(monkeypatch, [False, False, True, True])
    assert took < 5, "the bold read on after the player went"
    assert store.get_now_playing("speech") is None
    # Where it had got to, so ▶ picks up there.
    assert stops and stops[-1] >= 1


def test_one_idle_answer_is_not_the_end(store, monkeypatch):
    stops: list = []
    monkeypatch.setattr("agent_media_core.sinks.speech.mark_speech_stopped",
                        lambda sentence=None, tick=True: stops.append(sentence))
    monkeypatch.setattr(cli, "_REPLAY_ALIVE_EVERY_S", 0.05)
    said = iter([True, False, None, True, False])
    monkeypatch.setattr(cli, "_player_says_idle",
                        lambda target: next(said, False))
    _track()
    assert stops == []


def test_no_answer_is_not_the_end(store, monkeypatch):
    """Refusals are this lane's weather: only an answer ends a replay."""
    monkeypatch.setattr(cli, "_REPLAY_ALIVE_EVERY_S", 0.05)
    monkeypatch.setattr(cli, "_player_says_idle", lambda target: None)
    started = time.time()
    _track()
    assert time.time() - started > 0.4, "the replay was cut short"


def test_the_player_is_read_for_idle(monkeypatch):
    class _Sink:
        def __init__(self, snap):
            self.snap = snap

        def snapshot(self, target):
            return self.snap

    for snap, want in (({"idle-active": True, "pause": False}, True),
                       ({"idle-active": False}, False),
                       ({"pause": False}, None), ({}, None)):
        monkeypatch.setattr(cli, "SinkSpeech", lambda s=snap: _Sink(s))
        assert _READ_IDLE(cli.Target(name="phone")) is want
