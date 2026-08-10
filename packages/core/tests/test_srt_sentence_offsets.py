"""Reading a render's own word timings back as sentence boundaries.

This is how the phone lane gets exact sentence marks without a second render
or an extra round trip: edge-tts writes an SRT alongside the audio in the same
request, and the device that holds the clip turns it into `SENTENCE <idx>
<offset>` lines for the host that doesn't.

The cue stream is what the voice *said*, not what we wrote, so the alignment
has to tolerate drift — and refuse, rather than guess wrong, when it can't
find its footing.
"""

from __future__ import annotations

from agent_media_core.render.subtitles import parse_srt, sentence_offsets


def _srt(*blocks: tuple[str, str, str]) -> str:
    out = []
    for i, (start, end, text) in enumerate(blocks, 1):
        out.append(f"{i}\n{start} --> {end}\n{text}\n")
    return "\n".join(out)


# A real edge-tts response for this text (en-AU-NatashaNeural): the CLI emits
# SentenceBoundary cues when the voice reports them, so a cue is a whole
# sentence — note cue 2 starts *before* cue 1's end.
REAL = _srt(
    ("00:00:00,100", "00:00:03,512",
     "The first sentence runs a little longer than that."),
    ("00:00:03,462", "00:00:07,525",
     "The second has 42 items in it, e.g. these."),
    ("00:00:07,525", "00:00:11,025",
     "The third and final sentence closes this check."),
)

REAL_TEXT = ["The first sentence runs a little longer than that.",
             "The second has 42 items in it, e.g. these.",
             "The third and final sentence closes this check."]


def test_parses_a_real_response():
    cues = parse_srt(REAL)
    assert len(cues) == 3
    assert cues[0][0] == 0.1
    assert cues[1] == (3.462, 7.525, "The second has 42 items in it, e.g. these.")


def test_sentence_cues_map_straight_through():
    assert sentence_offsets(REAL, REAL_TEXT) == [0.0, 3.462, 7.525]


def test_word_cues_map_too():
    """Older voices report WordBoundary instead — one cue per word."""
    srt = _srt(("00:00:00,000", "00:00:00,400", "Hello"),
               ("00:00:00,400", "00:00:00,900", "there"),
               ("00:00:01,000", "00:00:01,500", "Goodbye"),
               ("00:00:01,500", "00:00:02,000", "now"))
    assert sentence_offsets(srt, ["Hello there.", "Goodbye now."]) == [0.0, 1.0]


def test_the_first_sentence_always_owns_the_head_of_the_clip():
    """Leading silence before the first word is still part of hearing it start."""
    srt = _srt(("00:00:00,750", "00:00:01,000", "Hello"))
    assert sentence_offsets(srt, ["Hello."]) == [0.0]


def test_expansions_do_not_derail_the_walk():
    """The voice says "forty two" for "42"; the cue stream is allowed to run
    ahead of our text without losing its place."""
    srt = _srt(("00:00:00,000", "00:00:00,300", "We"),
               ("00:00:00,300", "00:00:00,600", "counted"),
               ("00:00:00,600", "00:00:00,900", "forty"),
               ("00:00:00,900", "00:00:01,200", "two"),
               ("00:00:01,200", "00:00:01,500", "sheep"),
               ("00:00:02,000", "00:00:02,400", "Then"),
               ("00:00:02,400", "00:00:02,800", "slept"))
    offsets = sentence_offsets(srt, ["We counted 42 sheep.", "Then slept."])
    assert offsets == [0.0, 2.0]


def test_two_sentences_inside_one_cue_are_interpolated():
    """Our splitter merges short fragments; the voice may still separate them,
    and vice versa. A cue holding two of our sentences must still yield two
    distinct, ordered offsets rather than nothing."""
    srt = _srt(("00:00:00,000", "00:00:04,000", "One two three four."),
               ("00:00:04,000", "00:00:06,000", "Five six."))
    offsets = sentence_offsets(srt, ["One two.", "Three four.", "Five six."])
    assert offsets is not None
    assert offsets[0] == 0.0
    assert 1.5 < offsets[1] < 2.5           # halfway through the first cue
    assert offsets[2] == 4.0


def test_unrelated_text_is_refused():
    """Better no marks than marks pointing at the wrong words: the caller
    apportions the duration instead."""
    assert sentence_offsets(REAL, ["Nothing here matches.",
                                   "Nor does this one."]) is None


def test_a_sentence_the_voice_never_reached_is_refused():
    srt = _srt(("00:00:00,000", "00:00:01,000", "Hello there"))
    assert sentence_offsets(srt, ["Hello there.", "Goodbye now."]) is None


def test_nothing_to_read_is_not_an_error():
    assert sentence_offsets("", ["Hello."]) is None
    assert sentence_offsets(REAL, []) is None
    assert sentence_offsets("not an srt at all", ["Hello."]) is None
    assert sentence_offsets(REAL, ["...", "!!!"]) is None
    assert parse_srt("1\nbroken\ntext\n") == []
