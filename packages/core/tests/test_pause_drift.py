"""A pause has to stop the clock, on the lanes that follow one.

Speech that plays on another device is followed by timeline rather than by
polling the player: a read costs ~600ms over that link, and once it has been
slow the breaker refuses the next ones outright. The cost of that trade is
that a clock cannot notice the audio stopped — so the highlight read on
serenely through the silence and was several sentences ahead by the time
playback resumed.

Nothing has to observe the player to know: we are the ones pausing it.
"""

from __future__ import annotations

import time

import pytest

from agent_media_core import cli
from agent_media_core.intake.submit import elapsed_from_row
from agent_media_core.state import StateStore


@pytest.fixture
def row(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    s = StateStore()
    s.set_now_playing("speech", uri="remote-say:phone", started_at=1000.0,
                      target="phone",
                      extras={"play_started_at": time.time() - 5.0})
    return s


def _extras(s):
    return (s.get_now_playing("speech") or {}).get("extras") or {}


# --- the reading ------------------------------------------------------------

def test_a_running_reply_reads_the_clock():
    origin = time.time() - 3.0
    assert 2.9 < elapsed_from_row({"play_started_at": origin}, 0) < 3.2


def test_a_paused_reply_reads_where_it_stopped():
    """Frozen at the moment of the pause, however long ago that was."""
    origin = time.time() - 30.0
    ex = {"play_started_at": origin, "paused_at": origin + 2.0}
    assert elapsed_from_row(ex, 0) == pytest.approx(2.0)


def test_a_missing_origin_falls_back_rather_than_lurching():
    assert elapsed_from_row({}, time.time() - 1.0) == pytest.approx(1.0, abs=0.1)


# --- the stamping -----------------------------------------------------------

def test_pausing_freezes_and_resuming_credits_the_time_back(row):
    before = _extras(row)["play_started_at"]
    cli._stamp_speech_pause(True)
    paused_at = _extras(row).get("paused_at")
    assert paused_at, "the pause was not recorded"
    assert _extras(row)["live_pause"] is True
    held = elapsed_from_row(_extras(row), 0)
    time.sleep(0.3)
    assert elapsed_from_row(_extras(row), 0) == pytest.approx(held), \
        "the reading moved while the audio was stopped"
    cli._stamp_speech_pause(False)
    ex = _extras(row)
    assert "paused_at" not in ex and ex["live_pause"] is False
    assert ex["play_started_at"] > before, "the pause was not credited back"
    assert elapsed_from_row(ex, 0) == pytest.approx(held, abs=0.1), \
        "resuming jumped the reply forward by the length of the pause"


def test_a_cycle_flips_whatever_the_row_says(row):
    """The remote lane sends `cycle pause` rather than reading then writing —
    a read over that bridge is what used to make pause miss entirely — so the
    stamp has to flip from local state too."""
    cli._stamp_speech_pause()
    assert _extras(row).get("paused_at")
    cli._stamp_speech_pause()
    assert not _extras(row).get("paused_at")


def test_pausing_twice_does_not_move_the_origin(row):
    cli._stamp_speech_pause(True)
    first = _extras(row)["paused_at"]
    cli._stamp_speech_pause(True)
    assert _extras(row)["paused_at"] == first


def test_no_reply_in_flight_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    cli._stamp_speech_pause(True)      # must not raise
    assert StateStore().get_now_playing("speech") is None


# --- a pause we did not issue ----------------------------------------------

class _Pipe:
    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        return self._lines.pop(0) if self._lines else b""


class _Proc:
    def __init__(self, lines):
        self.stdout = _Pipe(lines)


def test_the_far_side_reports_a_pause_it_was_asked_for_by_someone_else(row):
    """A media key, the notification controls, MPRIS, a call. The renderer is
    already polling its own player locally at ~2ms a read and its report stream
    is still open, so it says so — the alternative is us polling a link where a
    read costs 600ms and trips a 45s breaker."""
    from agent_media_core.intake import submit

    submit._watch_remote_progress(
        _Proc([b"DURATION 30.0\n", b"PAUSE 1\n"]), row, "phone", 1000.0)
    ex = _extras(row)
    assert ex.get("paused_at"), "the far side's pause was not recorded"
    assert ex["live_pause"] is True
    held = elapsed_from_row(ex, 0)
    time.sleep(0.2)
    assert elapsed_from_row(_extras(row), 0) == pytest.approx(held)


def test_and_reports_the_resume(row):
    from agent_media_core.intake import submit

    submit._watch_remote_progress(
        _Proc([b"DURATION 30.0\n", b"PAUSE 1\n", b"PAUSE 0\n"]),
        row, "phone", 1000.0)
    assert "paused_at" not in _extras(row)
    assert _extras(row)["live_pause"] is False


def test_a_repeated_report_is_not_a_second_pause(row):
    from agent_media_core.intake import submit

    submit._watch_remote_progress(
        _Proc([b"DURATION 30.0\n", b"PAUSE 1\n"]), row, "phone", 1000.0)
    first = _extras(row)["paused_at"]
    submit.stamp_speech_pause(row, True)
    assert _extras(row)["paused_at"] == first


# --- the stamp has to survive the writer that does not own it ---------------
#
# The clip lane rebuilds the whole now-playing row on every mark, because
# everything on it belongs to the marking process. `paused_at` does not: the
# toggle stamps it from another process between two marks, and the rebuild wiped
# it about a second later — so the resume had no record of when the silence
# began, and `stamp_speech_pause`'s correction never ran on that lane.

from agent_media_core.intake.submit import (carry_pause_stamp,
                                            stamp_speech_pause)


def test_a_stamp_survives_a_rebuild_that_still_reads_paused():
    prior = {"paused_at": 1000.0}
    extras = {"live_pause": True}
    carry_pause_stamp(prior, extras, live_seen=True)
    assert extras["paused_at"] == 1000.0, (
        "the rebuild dated the pause to now, losing the silence before it")


def test_a_reading_that_says_playing_ends_the_pause():
    """A reading is fresher than a stamp: if the player says it is playing,
    the pause is over however recently it was stamped."""
    prior = {"paused_at": 1000.0}
    extras = {"live_pause": False}
    carry_pause_stamp(prior, extras, live_seen=True)
    assert "paused_at" not in extras


def test_no_reading_cannot_contradict_a_stamp():
    """A mark that did not poll the player knows nothing about the pause, so
    it must leave the stamp where it is rather than clear it by omission."""
    prior = {"paused_at": 1000.0}
    extras = {}
    carry_pause_stamp(prior, extras, live_seen=False)
    assert extras["paused_at"] == 1000.0


def test_a_pause_nobody_stamped_gets_dated_now():
    """The app's own transport and call-guard pause the player without going
    through the toggle. The row would otherwise say paused-since-never."""
    extras = {"live_pause": True}
    carry_pause_stamp({}, extras, live_seen=True)
    assert extras["paused_at"] == pytest.approx(time.time(), abs=2)


def test_the_resume_correction_works_again_end_to_end():
    """The point of all of it: with the stamp carried, a resume takes the
    silence off the clock instead of counting it as speech."""
    # Ten seconds of speech, then four seconds of silence, still held.
    now = time.time()
    origin = now - 14.0
    ex = {"play_started_at": origin, "paused_at": now - 4.0}

    class _State:
        def __init__(self, ex):
            self.ex = ex

        def get_now_playing(self, _sink):
            return {"uri": "u", "started_at": origin, "target": "app",
                    "extras": self.ex}

        def set_now_playing(self, _sink, **kw):
            self.ex = kw["extras"]

    st = _State(ex)
    held = elapsed_from_row(st.ex, 0)
    assert held == pytest.approx(10.0, abs=0.1)

    # ...a mark lands during the silence, rebuilding the row from scratch.
    rebuilt = {"play_started_at": st.ex["play_started_at"], "live_pause": True}
    carry_pause_stamp(st.ex, rebuilt, live_seen=True)
    st.ex = rebuilt
    assert elapsed_from_row(st.ex, 0) == pytest.approx(held, abs=0.1), \
        "the mark restarted the clock mid-pause"

    stamp_speech_pause(st, False)                   # resume, four seconds on
    assert elapsed_from_row(st.ex, 0) == pytest.approx(held, abs=0.3), \
        "the four seconds paused were counted as four seconds spoken"
