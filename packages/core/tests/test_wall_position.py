"""`live_pos_s` and `elapsed` have to be the same clock.

The live frame's `elapsed` is wall time since `play_started_at`, so it carries
the gaps between clips. `live_pos_s` — what `/speech/now`'s `pos` and the
progress bar ride — used to be summed clip lengths, which carries none of
them. The app reads the difference as staleness (`useElapsedSkew`) and holds
the bold back by it, so on a real reply the bold sat a whole sentence behind
the voice (David, 23 Sep 2026).

The numbers here are that reply, measured off the player: one 4.88s gap
between clip 0 and clip 1 (the first sentence is played alone while the rest
render), then a constant offset for every later boundary.
"""

from __future__ import annotations

from agent_media_core.intake.submit import wall_position

# clip_starts_s as the marks measured them, and the clip lengths that went
# with them.
STARTS = [0.0, 18.89, 25.61, 38.47]
DURATIONS = [14.02, 6.72, 12.86, 4.87]


def _extras(n: int) -> dict:
    """The row as `_stamp_start` leaves it after `n` sentences have begun."""
    return {"clip_starts_s": STARTS[:n]}


def test_the_position_is_where_the_voice_is_not_where_the_audio_sums_to():
    ex = _extras(2)
    wall_position(1, {"time-pos": 2.0}, ex, DURATIONS)
    # Summed durations would say 14.02 + 2.0 = 16.02, which is the gap short.
    assert ex["live_pos_s"] == 20.89


def test_the_gap_is_the_whole_difference():
    """Every later boundary carries the one gap, and no more."""
    for i in (1, 2, 3):
        ex = _extras(i + 1)
        wall_position(i, {"time-pos": 0.0}, ex, DURATIONS)
        nominal = sum(DURATIONS[:i])
        assert abs((ex["live_pos_s"] - nominal) - 4.87) < 0.02


def test_the_denominator_grows_by_the_gap_too():
    """Clamping a wall position against the summed audio pegged the bar at
    100% a gap before the reply ended."""
    ex = _extras(2)
    wall_position(1, {"time-pos": 0.0}, ex, DURATIONS)
    assert ex["total_wall_s"] == round(18.89 + 6.72 + 12.86 + 4.87, 3)
    assert ex["total_wall_s"] > sum(DURATIONS)


def test_a_sentence_with_no_measured_start_is_left_alone():
    """Only the marks know the wall clock; without one, write nothing rather
    than a position on the other timeline."""
    ex = _extras(1)
    wall_position(2, {"time-pos": 1.0}, ex, DURATIONS)
    assert "live_pos_s" not in ex and "total_wall_s" not in ex


def test_no_reading_still_dates_the_reply():
    """A mark with no player snapshot: the start is known, the position into
    the clip is not."""
    ex = _extras(2)
    wall_position(1, None, ex, DURATIONS)
    assert "live_pos_s" not in ex
    assert ex["total_wall_s"] == round(18.89 + 6.72 + 12.86 + 4.87, 3)


def test_a_reading_without_a_position_is_the_start_of_the_clip():
    ex = _extras(2)
    wall_position(1, {"pause": False}, ex, DURATIONS)
    assert ex["live_pos_s"] == 18.89
