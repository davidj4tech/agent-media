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
    monkeypatch.setattr(cli, "_replay_read_player", lambda target: {})


def _says(monkeypatch, answers):
    """The player answers idle (True), playing (False) or nothing (None)."""
    said = iter(answers)

    def read(target):
        v = next(said, answers[-1])
        return {} if v is None else {"idle-active": v, "pause": False}

    monkeypatch.setattr(cli, "_replay_read_player", read)


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
    monkeypatch.setattr(cli, "_REPLAY_ALIVE_EVERY_S", 0.05)
    _says(monkeypatch, answers)
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
    _says(monkeypatch, [True, False, None, True, False])
    _track()
    assert stops == []


def test_no_answer_is_not_the_end(store, monkeypatch):
    """Refusals are this lane's weather: only an answer ends a replay."""
    monkeypatch.setattr(cli, "_REPLAY_ALIVE_EVERY_S", 0.05)
    _says(monkeypatch, [None])
    started = time.time()
    _track()
    assert time.time() - started > 0.4, "the replay was cut short"


def test_the_player_is_read_for_idle():
    for snap, want in (({"idle-active": True, "pause": False}, True),
                       ({"idle-active": False}, False),
                       ({"pause": False}, None), ({}, None)):
        assert cli._player_says_idle(snap) is want


# ── The timeline, corrected by the player ───────────────────────────────

def test_the_first_clip_is_dated_by_the_player():
    """Pushed at 0, but the phone was 1.5 s into clip 0 at 3.0: it began at
    1.5, and the clips after it move with it."""
    seen: dict = {}
    got = cli._replay_anchor([0.0, 2.0, 4.0], [2.0, 2.0, 2.0],
                             {"playlist-pos": 0, "time-pos": 1.5,
                              "_read_at": 103.0}, 100.0, seen)
    assert got == [1.5, 3.5, 5.5]


def test_the_next_clip_follows_the_real_length():
    seen: dict = {}
    got = cli._replay_anchor([0.0, 2.0, 4.0], [2.0, 2.0, 2.0],
                             {"playlist-pos": 0, "time-pos": 1.0,
                              "duration": 5.0, "_read_at": 101.0}, 100.0, seen)
    assert got[0] == 0.0
    assert got[1] == 5.0 + cli_gap()      # the clip's own length, not 2.0
    assert got[2] == got[1] + 2.0         # still the guess: not heard yet


def test_the_step_between_clips_is_learned():
    seen: dict = {}
    base = 100.0
    cli._replay_anchor([0.0, 2.0, 4.0], [2.0, 2.0, 2.0],
                       {"playlist-pos": 0, "time-pos": 1.0, "duration": 3.0,
                        "_read_at": base + 1.0}, base, seen)
    got = cli._replay_anchor([0.0, 3.3, 5.3], [2.0, 2.0, 2.0],
                             {"playlist-pos": 1, "time-pos": 0.5,
                              "duration": 4.0, "_read_at": base + 4.0},
                             base, seen)
    # Clip 1 began at 3.5: clip 0 ran 3.0, so the step is 0.5.
    assert got[:2] == [0.0, 3.5]
    assert got[2] == 3.5 + 4.0 + 0.5


def test_speed_is_taken_off():
    got = cli._replay_anchor([0.0, 2.0], [2.0, 2.0],
                             {"playlist-pos": 0, "time-pos": 3.0, "speed": 1.5,
                              "duration": 6.0, "_read_at": 102.0}, 100.0, {})
    assert got == [0.0, 4.0 + cli_gap()]


def test_one_clip_moves_as_a_whole():
    got = cli._replay_anchor([0.0, 1.0, 2.5], [4.0],
                             {"playlist-pos": 0, "time-pos": 1.0,
                              "_read_at": 102.0}, 100.0, {})
    assert got == [1.0, 2.0, 3.5]


def test_paused_or_idle_readings_change_nothing():
    for snap in ({"playlist-pos": 0, "time-pos": 1.0, "_read_at": 1.0, "pause": True},
                 {"playlist-pos": 0, "time-pos": 1.0, "_read_at": 1.0, "idle-active": True},
                 {"playlist-pos": -1, "time-pos": 1.0, "_read_at": 1.0},
                 {"playlist-pos": 0, "_read_at": 1.0}):
        assert cli._replay_anchor([0.0, 1.0], [1.0, 1.0], snap, 0.0, {}) is None


def cli_gap():
    from agent_media_core.intake.submit import CLIP_GAP_S
    return CLIP_GAP_S


def test_the_row_takes_the_correction(store, monkeypatch):
    rows: list = []
    orig = store.set_now_playing

    def spy(sink, **kw):
        rows.append(dict(kw.get("extras") or {}))
        return orig(sink, **kw)

    monkeypatch.setattr(StateStore, "set_now_playing",
                        lambda self, sink, **kw: spy(sink, **kw))
    monkeypatch.setattr(cli, "_REPLAY_ALIVE_EVERY_S", 0.05)
    # Read once, just begun: the clip began at the reading, a moment after
    # the push.
    reading = {"idle-active": False, "pause": False, "playlist-pos": 0,
               "time-pos": 0.0, "_read_at": time.time() + 0.1}
    monkeypatch.setattr(cli, "_replay_read_player", lambda target: dict(reading))
    cli.cmd_replay_track(argparse.Namespace(
        sentences=cli.json.dumps(SENTS), offsets=cli.json.dumps([0.0, 0.2, 0.4]),
        pane="", durations=cli.json.dumps([0.2, 0.2, 0.2])))
    fixed = [r["clip_offsets_s"] for r in rows if "clip_offsets_s" in r]
    assert any(f[0] > 0.0 for f in fixed), "the first clip was not re-dated"


def test_a_pause_at_the_player_freezes_the_row(store, monkeypatch):
    rows: list = []
    orig = store.set_now_playing

    def spy(sink, **kw):
        rows.append(dict(kw.get("extras") or {}))
        return orig(sink, **kw)

    monkeypatch.setattr(StateStore, "set_now_playing",
                        lambda self, sink, **kw: spy(sink, **kw))
    monkeypatch.setattr(cli, "_REPLAY_ALIVE_EVERY_S", 0.05)
    monkeypatch.setattr(cli, "_replay_read_player", lambda target: {
        "idle-active": False, "pause": True, "playlist-pos": 0,
        "time-pos": 0.1, "_read_at": time.time()})
    import threading
    t = threading.Thread(target=_track, daemon=True)
    t.start()
    time.sleep(0.6)
    assert any(r.get("paused_by") == "replay-watch" for r in rows)
    ex = (store.get_now_playing("speech") or {}).get("extras") or {}
    ex.pop("paused_at", None)
    ex["writer_pid"] = None
    store.set_now_playing("speech", uri="x", started_at=time.time(),
                          target="phone", extras={})   # take it back: the follow ends
    t.join(timeout=5)
