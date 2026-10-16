"""A replay's `pos` (/speech/now, via popup-status) must be on the clock the
app's bold runs on.

A replay on the phone is followed on the clock: `play_started_at` and
`clip_offsets_s`, corrected by the player as it plays. The app compares that
clock with the `pos` the server reports and takes the difference for lag. The
`pos` summed `clip_durations_s` — on a replay, lengths guessed from the
characters — so it drifted off the clock by every guess's error, and the bold
ran 3-11 s ahead or up to 23 s behind the voice (16 Oct 2026).
"""

from agent_media_core import cli


def _row(monkeypatch, extras):
    monkeypatch.setattr(cli, "_now_speaking", lambda: {"extras": extras})


def test_a_replay_reads_its_clip_starts_on_the_clock(monkeypatch):
    _row(monkeypatch, {"total_duration_s": 15.0,
                       "clip_durations_s": [5.0, 5.0, 5.0],     # guessed
                       "clip_offsets_s": [0.0, 3.1, 6.4]})      # as played
    pos, dur, lifted = cli._as_response_timeline(1.0, 3.0, 2)
    assert lifted and dur == 15.0
    assert pos == 7.4, "pos summed the guessed lengths, off the reader's clock"


def test_without_a_clock_timeline_the_lengths_are_summed_as_before(monkeypatch):
    _row(monkeypatch, {"total_duration_s": 15.0,
                       "clip_durations_s": [5.0, 5.0, 5.0]})
    assert cli._as_response_timeline(1.0, 3.0, 2)[0] == 11.0


def test_sentence_offsets_inside_one_clip_are_not_clip_starts(monkeypatch):
    """The one-clip lane keeps sentence starts in `clip_offsets_s`; there the
    playlist position is always 0 and the sum is right."""
    _row(monkeypatch, {"total_duration_s": 9.0, "clip_durations_s": [9.0],
                       "clip_offsets_s": [0.0, 2.0, 5.0]})
    assert cli._as_response_timeline(4.0, 9.0, 0)[0] == 4.0
