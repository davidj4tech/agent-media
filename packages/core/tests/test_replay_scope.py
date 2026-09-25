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


def test_the_listeners_own_turn_is_never_replayed(env):
    """The app's replay said back David's question, not the answer
    (2026-09-25): his turn is filed as the newest speech row, for the archive.
    Replay and < / > walk the answers only."""
    env.add_history(sink="speech", uri="/you.mp3", started_at=9.0, ended_at=9.0,
                    target="none", source="listener", text="You: why?",
                    extras={"source_session": "aaa"})
    got = [r["text"] for r in cli._speech_history(10, session="aaa")]
    assert got[0] == "A3" and "You: why?" not in got


def _listener(st, at, session, text="You: why?", durations=(2.0,)):
    st.add_history(sink="speech", uri=f"/q{at}.mp3", started_at=at, ended_at=at,
                   target="none", source="listener", text=text,
                   extras={"source_session": session, "listener": True,
                           "clip_uris": [f"/q{at}.mp3"],
                           "clip_durations_s": list(durations)})


def _reply(st, at, session, text):
    st.add_history(sink="speech", uri=f"/r{at}.mp3", started_at=at, ended_at=at,
                   text=text, extras={"source_session": session,
                                      "clip_uris": [f"/r{at}.mp3"],
                                      "clip_durations_s": [3.0]})


def _row(text):
    return next(r for r in StateStore().recent_history(sink="speech", limit=50)
                if r["text"] == text)


def test_a_reply_is_replayed_after_its_question(env, monkeypatch):
    """David, 2026-09-25: hearing his message again is welcome — as long as
    the reply that answered it follows."""
    _listener(env, 10.0, "aaa")
    _reply(env, 11.0, "bbb", "B3 in between")      # another conversation
    _reply(env, 12.0, "aaa", "A4")
    assert cli._question_before(_row("A4"))["text"] == "You: why?"

    played = []
    monkeypatch.setattr(cli, "_replay_row",
                        lambda r, then_id=None: played.append((r["text"], then_id)) or 0)
    assert cli._do_replay(1, session="aaa") == 0
    assert played == [("You: why?", _row("A4")["id"])]


def test_only_the_first_reply_to_a_question_gets_it(env):
    _listener(env, 10.0, "aaa")
    _reply(env, 11.0, "aaa", "A4")
    _reply(env, 12.0, "aaa", "A5 a second reply")
    assert cli._question_before(_row("A5 a second reply")) is None
    # No question before it at all.
    assert cli._question_before(_row("A3")) is None


def test_a_question_with_no_timings_is_not_chained(env):
    _listener(env, 10.0, "aaa", durations=())
    _reply(env, 11.0, "aaa", "A4")
    assert cli._question_before(_row("A4")) is None


def _reply_of(st, at, session, text, n=4):
    st.add_history(sink="speech", uri=f"/r{at}-0.mp3", started_at=at, ended_at=at,
                   text=text,
                   extras={"source_session": session,
                           "clip_uris": [f"/r{at}-{i}.mp3" for i in range(n)],
                           "clip_sentences": [f"S{i}." for i in range(n)],
                           "clip_durations_s": [1.0] * n})


def _stopped(**kw):
    import json
    from agent_media_core.sinks import speech as sink
    p = sink._stopped_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"at": cli.time.time(), **kw}))


@pytest.fixture
def played(monkeypatch):
    got = []
    monkeypatch.setattr(cli, "_replay_row",
                        lambda r, from_sentence=None, then_id=None:
                        got.append((r["text"], from_sentence, then_id)) or 0)
    return got


def test_replay_soon_after_a_stop_picks_up_at_that_sentence(env, played):
    """David, 2026-09-25: Replay within five minutes of a Stop goes on from
    the sentence it was stopped on; the next Replay starts over."""
    _listener(env, 10.0, "aaa")
    _reply_of(env, 11.0, "aaa", "A4")
    _stopped(history_id=_row("A4")["id"], sentence=2)
    assert cli._do_replay(1, session="aaa") == 0
    assert played[-1] == ("A4", 2, None)
    assert cli._do_replay(1, session="aaa") == 0          # the stamp is spent
    assert played[-1] == ("You: why?", None, _row("A4")["id"])


def test_a_live_reply_is_known_by_its_start(env, played):
    _reply_of(env, 11.0, "aaa", "A4")
    _stopped(history_id=None, started_at=11.0, sentence=1)
    cli._do_replay(1, session="aaa")
    assert played[-1] == ("A4", 1, None)


def test_long_after_a_stop_it_starts_over_with_the_question(env, played):
    _listener(env, 10.0, "aaa")
    _reply_of(env, 11.0, "aaa", "A4")
    _stopped(history_id=_row("A4")["id"], sentence=2)
    from agent_media_core.sinks import speech as sink
    stamp = sink.last_stop()
    stamp["at"] -= 301
    sink._stopped_path().write_text(cli.json.dumps(stamp))
    cli._do_replay(1, session="aaa")
    assert played[-1] == ("You: why?", None, _row("A4")["id"])


def test_stopped_in_the_question_starts_from_the_question(env, played):
    _listener(env, 10.0, "aaa")
    _reply_of(env, 11.0, "aaa", "A4")
    _stopped(history_id=_row("You: why?")["id"], then_id=_row("A4")["id"],
             sentence=0)
    cli._do_replay(1, session="aaa")
    assert played[-1] == ("You: why?", None, _row("A4")["id"])


def test_a_stop_in_another_reply_is_not_this_ones(env, played):
    _reply_of(env, 11.0, "aaa", "A4")
    _stopped(history_id=_row("A3")["id"], sentence=2)
    cli._do_replay(1, session="aaa")
    assert played[-1] == ("A4", None, None)
