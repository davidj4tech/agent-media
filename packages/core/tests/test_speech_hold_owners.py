"""Multi-owner speech holds.

Speech used to be gated by one marker file, which was right while one thing
could ask for silence. Now several can — two named assistants (Sam on the
media speech channel, Cece in the phone app) and any number of Claude sessions
in tmux panes. With a single marker the second holder to release lifts the
first's hold and talks over it, so each holder gets its own marker and speech
stays held while ANY of them is live.
"""

import os
import time

import pytest

from agent_media_core.intake import submit


@pytest.fixture(autouse=True)
def _state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_SPEECH_HOLD_OWNER", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)


def test_holds_are_independent_and_releasing_one_leaves_the_others():
    submit.set_speech_hold(60, "cece")
    submit.set_speech_hold(60, "pane3")
    assert set(submit.speech_holders()) == {"cece", "pane3"}

    submit.release_speech_hold("cece")
    # The whole point: Cece finishing her turn must not un-hold the session
    # that is still speaking.
    assert set(submit.speech_holders()) == {"pane3"}
    assert submit.speech_hold_until() > 0.0


def test_speech_stays_held_until_the_last_owner_lifts():
    submit.set_speech_hold(60, "sam")
    submit.set_speech_hold(60, "cece")
    submit.release_speech_hold("sam")
    assert submit.speech_hold_until() > 0.0
    submit.release_speech_hold("cece")
    assert submit.speech_hold_until() == 0.0


def test_hold_until_is_the_latest_expiry_not_the_earliest():
    """A short hold expiring must not un-hold speech while a longer one runs."""
    short = submit.set_speech_hold(2, "cece")
    long_ = submit.set_speech_hold(120, "pane7")
    assert long_ > short
    assert submit.speech_hold_until() == pytest.approx(long_)


def test_an_expired_owner_is_reaped_on_read(monkeypatch):
    submit.set_speech_hold(60, "cece")
    marker = submit._owner_marker("cece")
    marker.write_text(repr(time.time() - 1))     # as if it lapsed
    assert submit.speech_holders() == {}
    # Reaped, not merely ignored — a dead session leaves no litter behind.
    assert not marker.exists()


def test_unnamed_holder_still_works_alongside_owners():
    """The pre-owner caller keeps working, and cannot lift a named hold."""
    submit.set_speech_hold(60)                   # legacy, no owner
    submit.set_speech_hold(60, "cece")
    assert set(submit.speech_holders()) == {"", "cece"}

    submit.release_speech_hold()                 # legacy release
    assert set(submit.speech_holders()) == {"cece"}


def test_release_everyone_is_the_only_way_to_drop_another_hold():
    submit.set_speech_hold(60)
    submit.set_speech_hold(60, "cece")
    submit.set_speech_hold(60, "pane3")
    submit.release_speech_hold(everyone=True)
    assert submit.speech_holders() == {}
    assert submit.speech_hold_until() == 0.0


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", ".", "x" * 33, "-lead"])
def test_owner_names_that_could_escape_the_directory_are_rejected(bad):
    """Owners arrive from callers (--owner), so they are untrusted on a path."""
    with pytest.raises(ValueError):
        submit.set_speech_hold(60, bad)


def test_expiry_is_still_capped(monkeypatch):
    """Per-owner holds inherit the mandatory cap — a forgotten hold from one
    session must not silence everyone for an hour."""
    monkeypatch.setenv("MEDIA_SPEECH_HOLD_MAX_S", "30")
    until = submit.set_speech_hold(9_999, "cece")
    assert until - time.time() <= 31


# --- owner resolution (CLI-side) --------------------------------------------

def test_owner_falls_back_to_the_tmux_pane(monkeypatch):
    """Several sessions each hold their own without inventing a name."""
    from agent_media_core.cli import _hold_owner
    monkeypatch.setenv("TMUX_PANE", "%42")
    assert _hold_owner(None) == "pane42"


def test_explicit_owner_beats_env_and_pane(monkeypatch):
    from agent_media_core.cli import _hold_owner
    monkeypatch.setenv("MEDIA_SPEECH_HOLD_OWNER", "fromenv")
    monkeypatch.setenv("TMUX_PANE", "%42")
    assert _hold_owner("cece") == "cece"
    assert _hold_owner(None) == "fromenv"


def test_no_tmux_no_env_means_the_unnamed_marker(monkeypatch):
    from agent_media_core.cli import _hold_owner
    assert _hold_owner(None) is None
