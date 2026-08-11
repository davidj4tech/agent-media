"""The status row: follow-along that costs a row of chrome, not of conversation.

It is the surface that works everywhere — inside a fullscreen TUI there is no
scrollback for the copy-mode highlight to search, and a split pane charges the
conversation rows on every reply.
"""

from __future__ import annotations

import argparse

import pytest

from agent_media_core import cli


def _args(**kw):
    return argparse.Namespace(**{"width": 80, "idle_hint": False, **kw})


@pytest.fixture
def speaking(monkeypatch):
    def _set(sentence):
        row = {"extras": {"current_sentence": sentence}} if sentence else None
        monkeypatch.setattr(cli, "_now_speaking", lambda: row)
    return _set


def _highlight(monkeypatch, on):
    from agent_media_core.intake import submit
    monkeypatch.setattr(submit, "_is_auto_highlight_enabled", lambda: on)


def test_the_spoken_sentence_is_the_row(speaking, capsys):
    speaking("This is what is being read.")
    cli.cmd_current_sentence(_args())
    assert capsys.readouterr().out.strip() == "♪ This is what is being read."


def test_it_collapses_and_truncates_to_the_row(speaking, capsys):
    speaking("word " * 40)
    cli.cmd_current_sentence(_args(width=20))
    out = capsys.readouterr().out.strip()
    assert len(out) <= 22 and out.endswith("…")


def test_idle_stays_blank_by_default(speaking, capsys):
    """The bar is quiet when nothing is happening — the long-standing rule."""
    speaking(None)
    cli.cmd_current_sentence(_args())
    assert capsys.readouterr().out == ""


def test_idle_can_answer_is_it_on(speaking, capsys, monkeypatch):
    """A permanent row has to say something between replies: whether following
    along is switched on at all is asked exactly then, when there is no
    sentence to show."""
    speaking(None)
    _highlight(monkeypatch, True)
    cli.cmd_current_sentence(_args(idle_hint=True))
    out = capsys.readouterr().out
    assert "follow-along on" in out
    assert "#[fg=" in out, "a status row styles with tmux formats, not ANSI"


def test_the_hint_is_silent_when_the_feature_is_off(speaking, capsys, monkeypatch):
    speaking(None)
    _highlight(monkeypatch, False)
    cli.cmd_current_sentence(_args(idle_hint=True))
    assert capsys.readouterr().out == ""


def test_a_playing_row_with_no_sentence_yet_is_not_a_crash(monkeypatch, capsys):
    """Between submit and the first sentence mark the row exists but carries
    nothing — the render lane's own gap."""
    monkeypatch.setattr(cli, "_now_speaking", lambda: {"extras": {}})
    _highlight(monkeypatch, True)
    cli.cmd_current_sentence(_args(idle_hint=True))
    assert "follow-along on" in capsys.readouterr().out
