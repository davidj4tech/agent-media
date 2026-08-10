"""Per-sentence state on a lane whose audio plays on another device.

The phone lane is one POST for the whole reply: the phone renders it and plays
it on its own mpv, so red5's sentence loop never runs and `current_sentence`
never got written — which is what follow-along highlight, `media
current-sentence` and the popup's sentence view all read.

Nothing here can watch that playback (a poll costs ~600ms on a link that drops
a quarter of its packets), so the renderer reports where each sentence starts
and a follower walks that timeline on the clock.
"""

from __future__ import annotations

import os
import time

from agent_media_core.intake import submit


class _Pipe:
    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        return self._lines.pop(0) if self._lines else b""

    def __iter__(self):
        raise AssertionError("must not iterate the renderer's pipe")


class _Proc:
    def __init__(self, lines):
        self.stdout = _Pipe(lines)


class _State:
    """Just enough store to be read back, so the follower can own a row."""

    def __init__(self):
        self.now_playing = None
        self.writes = 0

    def set_now_playing(self, sink, **kw):
        self.now_playing = (sink, kw)
        self.writes += 1

    def get_now_playing(self, sink):
        if not self.now_playing or self.now_playing[0] != sink:
            return None
        kw = self.now_playing[1]
        return {"uri": kw.get("uri"), "started_at": kw.get("started_at"),
                "target": kw.get("target"), "extras": dict(kw.get("extras") or {})}

    def extras(self):
        return (self.now_playing or (None, {}))[1].get("extras") or {}


def _follower(state, sentences, **kw):
    kw.setdefault("pane", "")
    kw.setdefault("highlight", False)
    kw.setdefault("delay_s", 0.0)
    return submit._SentenceFollower(state, "phone", 1000.0, sentences, **kw)


def _watch(state, lines, follower=None, report=None):
    submit._watch_remote_progress(_Proc(lines), state, "phone", 1000.0,
                                  report, follower)


# --- the wire ---------------------------------------------------------------

def test_marks_reach_the_row_as_a_sentence_timeline():
    state = _State()
    f = _follower(state, ["One.", "Two.", "Three."])
    _watch(state, [b"CLIP remote-1.mp3\n",
                   b"SENTENCE 0 0.0\n", b"SENTENCE 1 1.5\n", b"SENTENCE 2 4.0\n",
                   b"DURATION 6.0\n"], f)
    f.stop()
    ex = state.extras()
    assert ex["clip_sentences"] == ["One.", "Two.", "Three."]
    assert ex["clip_offsets_s"] == [0.0, 1.5, 4.0]
    assert ex["clip_durations_s"] == [1.5, 2.5, 2.0]
    assert ex["sentence_marks"] is True
    assert ex["current_sentence"] == "One."
    assert ex["current_sentence_idx"] == 0
    # The bar this lane already had must survive the addition.
    assert ex["total_duration_s"] == 6.0
    assert ex["clip_uris"] == ["remote-1.mp3"]


def test_reading_continues_past_duration():
    """DURATION used to end the watcher. Returning there closes our end of the
    pipe on a renderer that may still have something to say."""
    state = _State()
    pipe_lines = [b"DURATION 3.0\n", b"ERROR something later\n"]
    proc = _Proc(pipe_lines)
    submit._watch_remote_progress(proc, state, "phone", 1000.0, {}, None)
    assert proc.stdout._lines == [], "the watcher stopped reading at DURATION"


def test_a_second_duration_does_not_restart_the_timeline():
    state = _State()
    _watch(state, [b"DURATION 3.0\n", b"DURATION 99.0\n"])
    assert state.extras()["total_duration_s"] == 3.0


def test_marks_are_ignored_without_a_follower():
    """The rooms lane passes no follower; its row must look exactly as before."""
    state = _State()
    _watch(state, [b"SENTENCE 0 0.0\n", b"DURATION 2.0\n"])
    assert "clip_sentences" not in state.extras()


def test_junk_marks_are_skipped_not_fatal():
    state = _State()
    f = _follower(state, ["One.", "Two."])
    _watch(state, [b"SENTENCE x 1\n", b"SENTENCE 0\n",
                   b"SENTENCE 0 0.0\n", b"SENTENCE 1 1.0\n", b"DURATION 2.0\n"], f)
    f.stop()
    assert state.extras()["clip_offsets_s"] == [0.0, 1.0]


# --- what to trust ----------------------------------------------------------

def test_a_mark_per_sentence_is_required():
    """A count mismatch means the far side split the text differently from us —
    an older commit, most likely. Pointing confidently at the wrong words is
    worse than a smooth guess."""
    assert submit._offsets_from_marks({0: 0.0, 1: 1.0}, 3, 5.0) is None
    assert submit._offsets_from_marks({0: 0.0, 2: 1.0}, 2, 5.0) is None


def test_marks_must_be_ordered_and_inside_the_clip():
    assert submit._offsets_from_marks({0: 0.0, 1: 3.0, 2: 2.0}, 3, 9.0) is None
    assert submit._offsets_from_marks({0: 0.0, 1: 40.0}, 2, 9.0) is None
    assert submit._offsets_from_marks({0: 0.0, 1: 4.0}, 2, 9.0) == [0.0, 4.0]


def test_mismatched_marks_fall_back_to_approximation():
    state = _State()
    f = _follower(state, ["One.", "Two."])
    _watch(state, [b"SENTENCE 0 0.0\n", b"DURATION 8.0\n"], f)
    f.stop()
    ex = state.extras()
    assert ex["sentence_marks"] is False
    assert ex["clip_offsets_s"] == [0.0, 4.0]     # equal-length sentences


def test_no_marks_at_all_still_follows():
    """Stage one on its own: a renderer that only reports a duration still gets
    per-sentence state, apportioned by share of characters."""
    state = _State()
    f = _follower(state, ["Hi.", "A much longer sentence here."])
    _watch(state, [b"DURATION 10.0\n"], f)
    f.stop()
    offsets = state.extras()["clip_offsets_s"]
    assert offsets[0] == 0.0
    assert 0.9 < offsets[1] < 1.2                 # 3 of 31 characters


def test_apportioning_never_divides_by_zero():
    assert submit._apportioned_offsets([""], 4.0) == [0.0]


def test_no_timeline_without_a_duration():
    state = _State()
    f = _follower(state, ["One.", "Two."])
    assert f.timeline({0: 0.0, 1: 1.0}, 0.0) is None
    assert _follower(state, []).timeline({}, 5.0) is None


# --- following --------------------------------------------------------------

def test_the_follower_steps_sentences_on_the_clock():
    state = _State()
    state.set_now_playing("speech", uri="remote-say:phone", started_at=1000.0,
                          target="phone", extras={"writer_pid": None})
    f = _follower(state, ["One.", "Two.", "Three."])
    # Start it in the past so the whole timeline has already elapsed.
    f.start([0.0, 0.05, 0.1], time.time())
    deadline = time.time() + 3.0
    while time.time() < deadline and state.extras().get("current_sentence_idx") != 2:
        time.sleep(0.02)
    f.stop()
    assert state.extras()["current_sentence"] == "Three."
    assert state.extras()["current_sentence_idx"] == 2


def test_the_follower_re_bases_on_a_seek():
    """`media skip` seeks this lane by re-stamping the timeline's origin. A
    follower reading its own stale origin would drag the highlight back."""
    state = _State()
    state.set_now_playing("speech", uri="remote-say:phone", started_at=1000.0,
                          target="phone", extras={"writer_pid": None})
    f = _follower(state, ["One.", "Two.", "Three."])
    f.start([0.0, 10.0, 20.0], time.time())
    deadline = time.time() + 2.0
    while time.time() < deadline and state.extras().get("current_sentence") is None:
        time.sleep(0.02)
    assert state.extras()["current_sentence_idx"] == 0
    ex = state.get_now_playing("speech")["extras"]
    ex["play_started_at"] = time.time() - 20.0        # jumped to the last one
    state.set_now_playing("speech", uri="remote-say:phone", started_at=1000.0,
                          target="phone", extras=ex)
    deadline = time.time() + 2.0
    while time.time() < deadline and state.extras().get("current_sentence_idx") != 2:
        time.sleep(0.02)
    f.stop()
    assert state.extras()["current_sentence_idx"] == 2


def test_the_follower_lets_go_of_a_row_it_no_longer_owns():
    """A newer reply takes the row over; ours must not keep writing to it."""
    state = _State()
    state.set_now_playing("speech", uri="x", started_at=1.0, target="phone",
                          extras={"writer_pid": os.getpid() + 12345})
    f = _follower(state, ["One.", "Two."])
    f.start([0.0, 0.01], time.time() - 5)
    f.stop()
    assert "current_sentence" not in state.extras()


def test_stopping_does_not_resurrect_a_cleared_row():
    state = _State()
    f = _follower(state, ["One.", "Two."])
    f.start([0.0, 0.01], time.time() - 5)      # row is None: nothing to write
    f.stop()
    assert state.now_playing is None
