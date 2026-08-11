"""The follow pane rides the auto-highlight flag.

Following along is one wish, not two switches. The copy-mode highlight serves
it only in a pane with scrollback; the follow pane serves it anywhere. So
asking for the highlight asks for both — and turning it off puts both away.
"""

from __future__ import annotations

import pytest

from agent_media_core.intake import submit


@pytest.fixture
def spawned(monkeypatch):
    """Record what ensure_follow_view would have run, instead of running it."""
    calls: list = []

    def _popen(argv, **kw):
        calls.append((argv, kw.get("env") or {}))

        class _P:
            pid = 1234
        return _P()

    monkeypatch.setattr(submit.subprocess, "Popen", _popen)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.delenv("MEDIA_FOLLOW_AUTO", raising=False)
    return calls


def _highlight_on(monkeypatch, on: bool):
    monkeypatch.setattr(submit, "_is_auto_highlight_enabled", lambda: on)


def test_opening_follows_the_highlight_flag(spawned, monkeypatch):
    _highlight_on(monkeypatch, True)
    submit.ensure_follow_view(pane="%9")
    assert spawned, "the pane was never opened"
    argv, env = spawned[0]
    assert argv[0].endswith("media-follow-pane")
    assert env["MEDIA_FOLLOW_TARGET"] == "%9"


def test_speaking_opens_it_hands_off_but_a_press_may_take_a_window(
        spawned, monkeypatch):
    """`auto` declines to open a window you can't see; a deliberate press says
    where it went instead."""
    _highlight_on(monkeypatch, True)
    submit.ensure_follow_view()
    submit.ensure_follow_view(deliberate=True)
    assert [argv[1] for argv, _ in spawned] == ["auto", "open"]


def test_the_helper_is_found_off_a_hooks_minimal_path(monkeypatch):
    """A hook inherits /usr/bin and little else; ~/.local/bin is not on it."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert submit._follow_helper().endswith("media-follow-pane")


def test_nothing_opens_when_the_highlight_is_off(spawned, monkeypatch):
    _highlight_on(monkeypatch, False)
    submit.ensure_follow_view()
    assert spawned == []


def test_closing_does_not_need_the_flag(spawned, monkeypatch):
    """Turning the highlight off has already flipped the flag by the time we
    put the pane away — a close gated on it would never fire."""
    _highlight_on(monkeypatch, False)
    submit.ensure_follow_view(False)
    assert spawned[0][0][1] == "close"


def test_the_coupling_can_be_refused(spawned, monkeypatch):
    _highlight_on(monkeypatch, True)
    monkeypatch.setenv("MEDIA_FOLLOW_AUTO", "0")
    submit.ensure_follow_view()
    assert spawned == []


def test_no_tmux_no_pane(spawned, monkeypatch):
    _highlight_on(monkeypatch, True)
    monkeypatch.delenv("TMUX", raising=False)
    submit.ensure_follow_view()
    assert spawned == []


def test_a_missing_helper_is_not_an_error(monkeypatch):
    """A host without the tmux helpers installed still speaks."""
    _highlight_on(monkeypatch, True)
    monkeypatch.setenv("TMUX", "x")
    monkeypatch.delenv("MEDIA_FOLLOW_AUTO", raising=False)

    def _boom(*a, **kw):
        raise OSError("no such file")

    monkeypatch.setattr(submit.subprocess, "Popen", _boom)
    submit.ensure_follow_view()          # must not raise
