"""Subject/title resolution survives a pane that can't be resolved here.

The status bar and popup name the speaking pane through `_subject` /
`_subject_label`. When `source_pane` is a pane that's dead *on this server* —
renumbered by a tmux-resurrect restore, closed since, or living on another host
(a rooms hub) — tmux `display-message -t <id>` returns success with empty
fields, which used to leak through as a bare `↪` and a blank title. Now the
pane is liveness-checked and the title falls back to `source_window` captured at
speech time.
"""

import pytest

from agent_media_core import cli
from agent_media_core.intake import submit
from agent_media_core.state import StateStore


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("TTS_POPUP_PANE", raising=False)
    monkeypatch.delenv("MEDIA_STATUS_PANE", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    return StateStore()


def _now_playing_from(st, *, pane, window=None, sess="aaa"):
    extras = {"source_pane": pane, "source_tmux_session": "ts1",
              "source_session": sess}
    if window is not None:
        extras["source_window"] = window
    st.set_now_playing("speech", uri="/x.mp3", started_at=1.0, extras=extras)


# --- _subject: a dead pane is not a "different live pane" (no false ↪) ------

def test_dead_pane_does_not_follow(env, monkeypatch):
    _now_playing_from(env, pane="%0")
    monkeypatch.setattr(cli, "_caller_pane", lambda: "%3")     # caller != %0
    monkeypatch.setattr(cli, "_pane_alive", lambda p: p == "%3")  # %0 is dead
    pane, _sess, following = cli._subject()
    assert pane == "%0"
    assert following is False         # was True before the liveness guard


def test_live_other_pane_still_follows(env, monkeypatch):
    _now_playing_from(env, pane="%5")
    monkeypatch.setattr(cli, "_caller_pane", lambda: "%3")
    monkeypatch.setattr(cli, "_pane_alive", lambda p: True)     # %5 is live
    _pane, _sess, following = cli._subject()
    assert following is True


# --- _subject_label: stored title fills in for an unresolvable pane ---------

def test_label_falls_back_to_source_window(env, monkeypatch):
    _now_playing_from(env, pane="%0", window="dotfiles-refactoring")
    monkeypatch.setattr(cli, "_caller_pane", lambda: "%3")
    monkeypatch.setattr(cli, "_pane_alive", lambda p: p == "%3")  # %0 dead
    # No tmux lookup should run for a dead pane; make it explode if it does.
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: pytest.fail("queried a dead pane"))
    prefix, label = cli._subject_label()
    assert label == "dotfiles-refactoring"   # from source_window
    assert "↪" not in prefix                  # dead pane → not "following"


def test_label_names_the_clip_playing_not_the_pane_now(env, monkeypatch):
    # The pane has moved on to another conversation title while the queue is
    # still catching up. The bar must name what you're HEARING.
    _now_playing_from(env, pane="%3", window="title-when-it-was-said")
    monkeypatch.setattr(cli, "_caller_pane", lambda: "%3")
    monkeypatch.setattr(cli, "_pane_alive", lambda p: True)
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: pytest.fail("queried the live pane"))
    _prefix, label = cli._subject_label()
    assert label == "title-when-it-was-said"


def test_label_uses_live_window_when_idle(env, monkeypatch):
    # Nothing playing: the subject is your own pane, so its live name is right.
    monkeypatch.setattr(cli, "_caller_pane", lambda: "%3")
    monkeypatch.setattr(cli, "_pane_alive", lambda p: True)

    class _R:
        returncode = 0
        stdout = "live-window-name\t⠠ spinner pane title"

    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _R())
    _prefix, label = cli._subject_label()
    assert label == "live-window-name"


def test_label_empty_when_no_pane_and_no_stored(env, monkeypatch):
    # Status bar (no caller), nothing playing → clean ('', '') so the bar
    # falls back to the plain status instead of an empty progress bar.
    monkeypatch.setattr(cli, "_caller_pane", lambda: "")
    assert cli._subject_label() == ("", "")


# --- submit: the conversation title is captured while the pane is alive -----

def test_tmux_window_for_pane_prefers_window_name(monkeypatch):
    class _R:
        returncode = 0
        stdout = "my-conversation\t⠐ doing a thing"
    monkeypatch.setattr(submit.subprocess, "run", lambda *a, **k: _R())
    assert submit._tmux_window_for_pane("%7") == "my-conversation"


def test_tmux_window_for_pane_strips_spinner_when_window_unnamed(monkeypatch):
    class _R:
        returncode = 0
        stdout = "zsh\t⠐ real pane title"      # window name is just the shell
    monkeypatch.setattr(submit.subprocess, "run", lambda *a, **k: _R())
    assert submit._tmux_window_for_pane("%7") == "real pane title"


def test_tmux_window_for_pane_empty_outside_tmux(monkeypatch):
    assert submit._tmux_window_for_pane("") == ""
    assert submit._tmux_window_for_pane("#{pane_id}") == ""   # unexpanded literal
