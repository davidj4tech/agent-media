"""Popup < / > replay traversal scopes to the Claude *conversation*
(source_session), not the tmux session or pane.

The discriminating case mirrors real data: two conversations share one tmux
session, and one conversation spans two panes (a resume). Scope must follow the
conversation: keep both of its panes, exclude the sibling conversation.
"""

import pytest

from agent_media_core import cli
from agent_media_core.state import StateStore


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("TTS_POPUP_PANE", raising=False)
    st = StateStore()
    # Conversation A (claude=aaa) spans panes %1 and %27, both tmux session "ts1".
    # Conversation B (claude=bbb) is pane %30, *same* tmux session "ts1".
    rows = [
        ("A1 pre-resume", "%1",  "ts1", "aaa"),
        ("B1",            "%30", "ts1", "bbb"),
        ("A2 post-resume","%27", "ts1", "aaa"),
        ("B2",            "%30", "ts1", "bbb"),
        ("A3",            "%27", "ts1", "aaa"),
    ]
    for i, (text, pane, tmux, claude) in enumerate(rows):
        st.add_history(sink="speech", uri=f"/{i}.mp3", started_at=float(i),
                       ended_at=float(i), text=text,
                       extras={"source_pane": pane, "source_tmux_session": tmux,
                               "source_session": claude,
                               "clip_uris": [f"/{i}.mp3"]})
    return st


def test_scope_follows_conversation_not_tmux_session(env, monkeypatch):
    # Anchor on conversation A; should see A1/A2/A3 (both panes), never B.
    got = [r["text"] for r in cli._speech_history(10, session="aaa")]
    assert got == ["A3", "A2 post-resume", "A1 pre-resume"]   # newest-first
    assert all("B" not in t for t in got)

    # Conversation B is isolated even though it shares tmux session "ts1".
    gotB = [r["text"] for r in cli._speech_history(10, session="bbb")]
    assert gotB == ["B2", "B1"]


def test_unscoped_returns_everything(env):
    assert len(cli._speech_history(10)) == 5


def test_anchor_session_follows_now_playing_conversation(env):
    env.set_now_playing("speech", uri="/x.mp3", started_at=9.0,
                        extras={"source_pane": "%27", "source_tmux_session": "ts1",
                                "source_session": "aaa"})
    assert cli._anchor_session() == "aaa"


def test_anchor_session_idle_resolves_caller_pane_conversation(env, monkeypatch):
    # Idle (no now_playing) + popup opened from %27 → conversation A.
    monkeypatch.setenv("TTS_POPUP_PANE", "%27")
    assert cli._anchor_session() == "aaa"
