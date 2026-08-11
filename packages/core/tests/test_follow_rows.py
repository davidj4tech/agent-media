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
    assert rows == [], "rows were taken for a sentence you can already see"


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
    assert rows == []


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
