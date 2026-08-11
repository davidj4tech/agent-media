"""The status rows appear only when the words are unreachable.

A row of the terminal isn't free, and most of the time it is redundant: the
sentence being spoken is right there in the pane. The signal for "it isn't" is
already paid for — copy-mode searches a normal pane's scrollback and an
alt-screen pane's visible view, so a failed search means the reader cannot see
these words by any means the pane offers.
"""

from __future__ import annotations

import pytest

from agent_media_core.intake import submit


@pytest.fixture(autouse=True)
def _following(monkeypatch):
    """Follow-along switched on, unless a test says otherwise."""
    monkeypatch.setattr(submit, "_is_auto_highlight_enabled", lambda: True)


@pytest.fixture
def rows(monkeypatch):
    """Record the row heights asked for, instead of resizing a real session."""
    asked: list = []
    monkeypatch.setattr(submit, "_set_follow_rows",
                        lambda show, pane="": asked.append(show))
    return asked


@pytest.fixture
def found(monkeypatch):
    """Control whether the highlight can find the sentence."""
    def _set(ok):
        monkeypatch.setattr(submit, "_tmux_highlight_text",
                            lambda *a, **kw: ok)
    return _set


def test_visible_text_costs_no_rows(rows, found):
    found(True)
    h = submit._HighlightScheduler(0.0, True, "%1")
    h.show("Something on screen.", first=True, force=False)
    h.drain()
    assert True not in rows, "rows were taken for a sentence you can already see"


def test_unreachable_text_takes_the_rows_and_gives_them_back(rows, found):
    found(False)
    h = submit._HighlightScheduler(0.0, True, "%1")
    h.show("Something off screen.", first=True, force=False)
    assert rows == [True]
    h.drain()
    assert rows == [True, False]


def test_the_rows_latch_for_the_rest_of_the_reply(rows, found):
    """Flipping per sentence would resize the panes — and make a fullscreen TUI
    redraw — every time the view scrolled. One reply, one decision."""
    found(False)
    h = submit._HighlightScheduler(0.0, True, "%1")
    h.show("First, off screen.", first=True, force=False)
    found(True)
    h.show("Second, on screen.", first=False, force=False)
    h.show("Third, on screen.", first=False, force=False)
    assert rows == [True], "the rows were re-asked mid-reply"
    h.drain()
    assert rows == [True, False]


def test_nothing_happens_while_following_along_is_off(rows, found):
    found(False)
    h = submit._HighlightScheduler(0.0, False, "%1")
    h.show("Off screen, but nobody asked to follow.", first=True, force=False)
    h.drain()
    assert True not in rows


def test_a_deferred_highlight_still_reports(rows, found, monkeypatch):
    """The rooms lane defers the highlight onto a timer to land with the audio;
    the answer arrives late but must still arrive."""
    found(False)
    h = submit._HighlightScheduler(0.01, True, "%1")
    h.show("Off screen, deferred.", first=True, force=False)
    h.drain()                      # joins the timer, then releases the rows
    assert rows == [True, False]


def test_one_row_is_spelled_on(monkeypatch):
    """tmux takes off|on|2..5 — `1` is an error, not a synonym, and reading it
    back as a bare int makes one row unrepresentable."""
    calls: list = []

    class _R:
        returncode = 0
        stdout = "on"

    monkeypatch.setattr(submit.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or _R())
    assert submit._status_rows("s") == 1


def test_the_bar_does_not_grow_while_follow_along_is_off(monkeypatch):
    """The scheduler's `enabled` is about this turn; whether the feature is on
    at all is the flag, which _tmux_highlight_text checks separately. With it
    off every sentence came back "not found" and the bar grew four rows to show
    nothing — the rows are silent when the feature is off."""
    monkeypatch.setattr(submit, "_is_auto_highlight_enabled", lambda: False)
    monkeypatch.setenv("TMUX", "x")
    calls: list = []
    monkeypatch.setattr(submit.subprocess, "run",
                        lambda argv, **kw: calls.append(argv))
    submit._set_follow_rows(True, "%1")
    assert calls == []


def test_giving_the_rows_back_is_never_refused(monkeypatch):
    """Turning the feature off has already flipped the flag by the time we put
    the rows away; a close gated on it would strand the bar four rows tall."""
    monkeypatch.setattr(submit, "_is_auto_highlight_enabled", lambda: False)
    monkeypatch.setenv("TMUX", "x")
    seen: list = []

    class _R:
        returncode = 0
        stdout = "5"

    def _run(argv, **kw):
        seen.append(argv)
        return _R()

    monkeypatch.setattr(submit.subprocess, "run", _run)
    submit._set_follow_rows(False, "%1")
    assert any("status" in a for a in seen), "the rows were never handed back"


def test_turning_it_on_mid_reply_revives_the_skipped_turn(rows, found, monkeypatch):
    """The keystroke skip disables the scheduler for a reply you typed into a
    moment ago — which is every reply. Pressing `v` (or prefix V) mid-read is
    the statement that you have stopped, so the rest of the reply follows."""
    from agent_media_core.intake import submit as S
    found(False)
    forced = {"on": False}
    monkeypatch.setattr(S, "_force_highlight_active", lambda p: forced["on"])

    h = S._HighlightScheduler(0.0, False, "%1")     # skipped this turn
    h.show("First sentence.", first=True, force=False)
    assert rows == [], "a skipped turn should stay quiet"

    forced["on"] = True                              # ← the press
    h.show("Second sentence.", first=False, force=False)
    assert rows == [True], "the rest of the reply did not start following"


def test_the_rows_are_handed_back_even_if_someone_else_opened_them(rows, found):
    """The popup opens them directly when you turn follow-along on mid-reply,
    outside the scheduler's knowledge. Rows nobody closes stay open."""
    from agent_media_core.intake import submit as S
    found(True)
    h = S._HighlightScheduler(0.0, True, "%1")
    h.show("On screen.", first=True, force=False)
    h.drain()
    assert rows == [False]


# --- the three heights ------------------------------------------------------

@pytest.fixture
def height(monkeypatch):
    """Capture the status height asked for, without touching a real session."""
    asked: list = []

    class _R:
        returncode = 0
        stdout = "on"

    def _run(argv, **kw):
        if "set" in argv and "status" in argv:
            asked.append(argv[-1])
        return _R()

    monkeypatch.setenv("TMUX", "x")
    monkeypatch.setenv("MEDIA_FOLLOW_ROWS", "4")
    monkeypatch.setattr(submit.subprocess, "run", _run)
    return asked


def test_following_along_owns_one_row_even_with_nothing_playing(height):
    """The switch is usually thrown between replies, where there is no
    sentence to fail to find — and a switch with no visible effect is
    indistinguishable from a broken one. The row says it is on."""
    submit._set_follow_rows(False, "%1")
    assert height == ["2"]


def test_unreachable_words_take_the_full_height(height):
    submit._set_follow_rows(True, "%1")
    assert height == ["5"]


def test_switched_off_is_the_bare_bar(height, monkeypatch):
    monkeypatch.setattr(submit, "_is_auto_highlight_enabled", lambda: False)
    monkeypatch.setattr(submit, "_status_rows", lambda s: 5)   # was expanded
    submit._set_follow_rows(False, "%1")
    assert height == ["on"]          # tmux spells one row `on`


def test_a_height_already_set_is_not_reset(height, monkeypatch):
    """Every set redraws the client, and the redraw re-runs the commands that
    render the bar."""
    monkeypatch.setattr(submit, "_status_rows", lambda s: 2)
    submit._set_follow_rows(False, "%1")
    assert height == []
