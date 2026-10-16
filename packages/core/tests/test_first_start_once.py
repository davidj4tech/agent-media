"""Sentence 0's start is dated once, from the phone's own reading.

17 Oct 2026, thread 0f14b788: the voice began sentence 0 at 2.0 s and 1 at
5.5 s; red5 stamped both 5.4 — a torn reading (position 0, sentence 1's
playhead) moved sentence 0 later, and sentence 1 cannot start before it. The
app read the 3.4 s as the voice ahead and kept the bold ahead all reply.
"""

from agent_media_core.intake.submit import first_start_reading


def test_only_the_first_reading_dates_it():
    clock = {}
    assert first_start_reading(clock, {"time-pos": 1.0, "duration": 3.6})
    # The torn one: still "position 0", sentence 1's playhead and length.
    assert not first_start_reading(clock, {"time-pos": 0.3, "duration": 9.1})


def test_a_guess_without_a_clip_length_does_not_count():
    """The frames model runs the playhead on from the load before the phone
    has said anything; it carries no length until it has."""
    clock = {}
    assert not first_start_reading(clock, {"time-pos": 2.2, "duration": -1.0})
    assert not first_start_reading(clock, {"time-pos": 0.1, "duration": 3.6})
    assert not first_start_reading(clock, None)
    assert first_start_reading(clock, {"time-pos": 0.6, "duration": 3.6})
