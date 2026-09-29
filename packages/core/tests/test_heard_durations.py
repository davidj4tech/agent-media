"""A phone-voiced reply's timeline from the phone's own clip lengths.

The server only guesses how long a clip the phone voices will be, from its
characters; the phone knows once it plays it. The next sentence starts that
long after this one did, plus the step between clips this reply has shown
(David, 29 Sep 2026: "follow along is still a little bit off").
"""

from agent_media_core.intake import submit as S


def test_guesses_stand_until_the_player_says():
    assert S.heard_durations([2.0, 3.0], {}, [0.0]) == [2.0, 3.0]


def test_a_reported_clip_takes_the_default_step_before_any_is_measured():
    got = S.heard_durations([2.0, 3.0, 4.0], {0: 4.5}, [0.0])
    assert got == [4.5 + S.CLIP_GAP_S, 3.0, 4.0]


def test_the_step_is_learned_from_the_clips_heard_out():
    # Clip 0 ran 4.0 s and the next began at 4.6; clip 1 ran 2.0 and the
    # next began 2.8 later: steps of 0.6 and 0.8.
    got = S.heard_durations([9.0, 9.0, 9.0, 9.0], {0: 4.0, 1: 2.0, 2: 3.0},
                            [0.0, 4.6, 7.4])
    assert got[:3] == [4.8, 2.8, 3.8]    # median of [0.6, 0.8] is 0.8
    assert got[3] == 9.0


def test_a_late_reading_cannot_make_the_step_negative():
    got = S.heard_durations([1.0, 1.0], {0: 5.0}, [0.0, 3.0])
    assert got[0] == 5.0
