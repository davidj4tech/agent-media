"""The follow-along pane's layout.

It exists for the case the copy-mode highlight cannot serve — a fullscreen TUI
holds the alternate screen, so there is no scrollback to search — which means
its correctness is exactly "the sentence being spoken is on screen, and looks
different from the ones that aren't".
"""

from __future__ import annotations

from agent_media_core.follow import frame, idle_frame, layout


SENTENCES = [f"Sentence number {i} with enough words in it to wrap a narrow "
             f"pane at least once." for i in range(20)]


def _plain(lines):
    return [ln.replace("\x1b[2m", "").replace("\x1b[1m", "")
              .replace("\x1b[0m", "") for ln in lines]


def test_a_frame_is_exactly_the_pane():
    lines = frame(SENTENCES, 5, width=40, height=12, colour=False)
    assert len(lines) == 12
    assert all(len(ln) <= 40 for ln in lines)


def test_the_current_sentence_is_on_screen():
    for idx in (0, 7, len(SENTENCES) - 1):
        lines = _plain(frame(SENTENCES, idx, width=40, height=12))
        body = " ".join(ln.strip() for ln in lines)
        assert f"Sentence number {idx} " in body, f"sentence {idx} scrolled off"


def test_spoken_current_and_waiting_look_different():
    lines = frame(SENTENCES, 5, width=48, height=16)
    marked = [ln for ln in lines if "\x1b[1m" in ln]
    dimmed = [ln for ln in lines if "\x1b[2m" in ln]
    assert marked, "the current sentence carries no mark"
    assert dimmed, "already-spoken sentences are not dimmed"
    assert "Sentence number 5 " in " ".join(marked)
    assert "Sentence number 4 " in " ".join(dimmed)
    # A sentence not yet spoken is plain: no escape at all.
    plain = [ln for ln in lines if "\x1b[" not in ln and ln.strip()]
    assert "Sentence number 6" in " ".join(plain) or "wrap" in " ".join(plain)


def test_the_last_sentence_does_not_scroll_into_blank_pane():
    """Anchoring the current sentence part-way down the pane must not run the
    view past the end of the text while the voice is still reading."""
    lines = _plain(frame(SENTENCES, len(SENTENCES) - 1, width=40, height=12))
    assert lines[-1].strip() != "", "the view ran off the end of the reply"


def test_colour_can_be_refused():
    lines = frame(SENTENCES, 3, width=40, height=8, colour=False)
    assert not any("\x1b[" in ln for ln in lines)


def test_nothing_playing_shows_the_top():
    lines = _plain(frame(SENTENCES, None, width=40, height=8))
    assert "Sentence number 0" in " ".join(lines)


def test_a_single_short_reply_fits():
    lines = frame(["Just this."], 0, width=40, height=6, colour=False)
    assert len(lines) == 6
    assert "Just this." in lines[0]


def test_no_sentences_is_not_a_crash():
    assert len(frame([], None, width=40, height=5, colour=False)) == 5
    assert len(frame([""], 0, width=40, height=5, colour=False)) == 5


def test_a_tiny_pane_still_renders():
    lines = frame(SENTENCES, 2, width=1, height=1, colour=False)
    assert len(lines) == 3          # floored, not crashed
    assert all(len(ln) <= 12 for ln in lines)


def test_sentences_are_separated_by_a_blank_line():
    lines, starts = layout(["One.", "Two."], 40)
    assert lines == ["One.", "", "Two."]
    assert starts == [0, 2]


def test_idle_keeps_the_last_reply_on_screen():
    lines = _plain(idle_frame("The reply that just finished.", 40, 6))
    assert len(lines) == 6
    assert "The reply that just finished." in " ".join(lines)


def test_idle_with_nothing_ever_spoken_says_so():
    assert "nothing spoken yet" in " ".join(_plain(idle_frame("", 40, 4)))
