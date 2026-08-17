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


def test_anchor_survives_a_conversation_going_quiet(env, monkeypatch):
    """A pane's conversation must resolve however long ago it last spoke.

    The regression: the idle branch scanned the 50 most recent clips globally,
    so once enough other conversations spoke, the pane fell out of the window,
    the anchor resolved to None and every `--session` view silently widened to
    all conversations. Observed for real on 2026-08-17 — this session had been
    quiet for three days with 285 clips on top of it, and its clip list showed
    everybody's.
    """
    monkeypatch.setenv("TTS_POPUP_PANE", "%27")
    for i in range(200):
        env.add_history(sink="speech", uri=f"/noise{i}.mp3",
                        started_at=100.0 + i, ended_at=100.0 + i,
                        text=f"someone else {i}",
                        extras={"source_pane": "%99", "source_tmux_session": "ts2",
                                "source_session": "zzz",
                                "clip_uris": [f"/noise{i}.mp3"]})
    assert cli._anchor_session() == "aaa"


def test_anchor_prefers_the_last_conversation_in_a_reused_pane(env, monkeypatch):
    """tmux recycles pane ids, so a pane outlives its conversations.

    One observed pane (%64) had carried twelve. The newest speaker is the only
    answer this table can give, so assert that rather than pretending the pane
    identifies a conversation.
    """
    monkeypatch.setenv("TTS_POPUP_PANE", "%27")
    env.add_history(sink="speech", uri="/later.mp3", started_at=500.0,
                    ended_at=500.0, text="a later conversation, same pane",
                    extras={"source_pane": "%27", "source_tmux_session": "ts1",
                            "source_session": "ccc", "clip_uris": ["/later.mp3"]})
    assert cli._anchor_session() == "ccc"


def test_anchor_ignores_clips_with_no_conversation(env, monkeypatch):
    """Untagged clips in the pane must not shadow the tagged one below them."""
    monkeypatch.setenv("TTS_POPUP_PANE", "%27")
    env.add_history(sink="speech", uri="/untagged.mp3", started_at=400.0,
                    ended_at=400.0, text="no source_session here",
                    extras={"source_pane": "%27", "clip_uris": ["/untagged.mp3"]})
    assert cli._anchor_session() == "aaa"


def test_anchor_none_for_an_unexpanded_pane_literal(env, monkeypatch):
    monkeypatch.setenv("TTS_POPUP_PANE", "#{pane_id}")
    assert cli._anchor_session() is None


# ---- pane ownership --------------------------------------------------------
#
# The clip history can only answer "who spoke here last", and tmux recycles pane
# ids — one observed pane had carried twelve conversations. Ownership is
# recorded when a session starts, by agent-config's claude-tmux-session-register
# hook, so it is right for a live conversation that has said nothing yet and
# does not decay when a pane is reused.

@pytest.fixture
def registry(tmp_path, monkeypatch):
    d = tmp_path / "tmux-sessions"
    d.mkdir()
    monkeypatch.setenv("MEDIA_PANE_REGISTRY_DIR", str(d))

    def write(pane, session, pid="self"):
        import os as _os
        pid = _os.getpid() if pid == "self" else pid
        body = f"{session} {pid} /home/x/proj" if pid is not None else session
        (d / pane.lstrip("%")).write_text(body)

    return write


def test_ownership_beats_the_last_speaker_in_a_reused_pane(env, monkeypatch, registry):
    """The case clip history gets wrong: %27 last spoke as conversation A, but
    the pane now belongs to B."""
    monkeypatch.setenv("TTS_POPUP_PANE", "%27")
    assert cli._anchor_session() == "aaa"          # by clip history alone
    registry("%27", "bbb")
    assert cli._anchor_session() == "bbb"


def test_a_dead_owner_is_not_an_owner(env, monkeypatch, registry):
    """A registry entry outlives the session it names, and a recycled pane will
    have one. An exited pid owns nothing, so fall back to who spoke here."""
    monkeypatch.setenv("TTS_POPUP_PANE", "%27")
    registry("%27", "bbb", pid=999_999_999)
    assert cli._anchor_session() == "aaa"


def test_legacy_bare_session_id_is_accepted(env, monkeypatch, registry):
    """The registry's older shape carries no pid — trust it rather than ignore
    a pane whose owner simply predates the pid being recorded."""
    monkeypatch.setenv("TTS_POPUP_PANE", "%27")
    registry("%27", "bbb", pid=None)
    assert cli._anchor_session() == "bbb"


def test_a_silent_owner_does_not_kill_the_keybinding(env, monkeypatch, registry):
    """Ownership wins only when there is something to traverse. A conversation
    that has not spoken yet would otherwise scope every popup key to an empty
    set, which is a dead keybinding; showing this pane's own past is better."""
    monkeypatch.setenv("TTS_POPUP_PANE", "%27")
    registry("%27", "never-spoke")
    assert cli._anchor_session() == "aaa"


def test_now_playing_still_wins_over_ownership(env, monkeypatch, registry):
    """What you are hearing outranks where you are sitting."""
    monkeypatch.setenv("TTS_POPUP_PANE", "%27")
    registry("%27", "bbb")
    env.set_now_playing("speech", uri="/x.mp3", started_at=9.0,
                        extras={"source_pane": "%30", "source_tmux_session": "ts1",
                                "source_session": "aaa"})
    assert cli._anchor_session() == "aaa"
