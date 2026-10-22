"""Disposable thinking uses the real submission loop, with no real players."""
from pathlib import Path

import pytest

from agent_media_core import book_tracks
from agent_media_core.intake import submit as S
from agent_media_core.state import StateStore
from agent_media_core.types import Event, Priority, Source, Target


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    import os
    for name in list(os.environ):
        if name.startswith(("MEDIA_", "RELAY_")):
            monkeypatch.delenv(name, raising=False)
    for name in ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("MEDIA_EARCONS", "0")
    monkeypatch.setenv("MEDIA_FOLLOW_AUTO", "0")
    monkeypatch.setattr(S, "render_text", render)
    monkeypatch.setattr(S, "_clip_duration", lambda p: 0.0)
    monkeypatch.setattr(S, "_gate_hold", lambda *a: False)
    monkeypatch.setattr(S, "_busy_hold", lambda *a: None)
    monkeypatch.setattr(S, "_ringer_hold", lambda *a: None)
    monkeypatch.setattr(S, "_remote_playlist", lambda *a: True)
    monkeypatch.setattr(S, "_source_place", lambda *a: ("", ""))


def render(text, outfile, **kw):
    Path(outfile).write_bytes(b"fake audio")
    return True, ""


class Coord:
    def cancel_pre_pause(self): pass
    def pre_pause_remote(self): raise AssertionError("thinking pre-paused media")
    def before_speech(self, **kw): pass
    def after_speech(self): pass
    def speaking_line(self, *a): pass


class Sink:
    def __init__(self, on_play=lambda: None):
        self.played = []
        self.on_play = on_play

    def play(self, uri, target, **kw):
        self.played.append(uri)
        self.on_play()

    def idle(self, target): return True
    def paused(self, target): return False
    def stop(self, target): pass
    def load_playlist(self, *a): raise AssertionError("thinking loaded an autonomous playlist")
    def claim_and_play(self, *a): raise AssertionError("thinking started an autonomous playlist")


def say(sink, allowed=lambda: True):
    return S.submit_event(
        Event("First thought is long enough to be its own sentence. Another thought should be spoken only when still enabled.", Source.WATCHER, Priority.LOW,
              target=Target("local"), metadata={"session": "thinking-test", "thinking": True,
                                               "thinking_message": "m", "thinking_part": 0}),
        state=StateStore(), sink=sink, coordinator=Coord(), playback_allowed=allowed)


def test_uses_sentence_playback_and_never_adds_a_reply():
    sink = Sink()
    assert say(sink) is None
    assert len(sink.played) == 2
    assert StateStore().recent_history(sink="speech") == []
    assert all(not Path(p).exists() for p in sink.played)
    assert StateStore().get_now_playing("speech") is None


def test_toggle_off_finishes_current_sentence_only():
    enabled = [True]
    sink = Sink(lambda: enabled.__setitem__(0, False))
    say(sink, lambda: enabled[0])
    assert len(sink.played) == 1


@pytest.mark.parametrize("priority", [Priority.NORMAL, Priority.HIGH])
def test_same_session_reply_or_question_drops_remaining_thinking(priority):
    waiting = S._SpeechPlaybackLock(speaker="thinking-test")
    sink = Sink(lambda: waiting.announce(priority, session="thinking-test"))
    try:
        say(sink)
        assert len(sink.played) == 1
    finally:
        waiting.release()


def test_busy_player_drops_thinking_instead_of_playing_unserialized():
    holder = S._SpeechPlaybackLock()
    holder.acquire()
    try:
        sink = Sink()
        say(sink)
        assert sink.played == []
    finally:
        holder.release()


def test_pending_final_reply_drops_thinking_before_playback():
    final = S._SpeechPlaybackLock(speaker="thinking-test")
    final.announce(Priority.NORMAL, session="thinking-test")
    try:
        sink = Sink()
        say(sink)
        assert sink.played == []
    finally:
        final.release()


@pytest.mark.parametrize("reason", ["toggle", "hold", "gate", "ringer", "busy", "mute"])
def test_ineligible_thinking_is_not_played_or_saved(monkeypatch, reason):
    if reason == "hold":
        monkeypatch.setattr(S, "speech_hold_until", lambda: 100)
    if reason == "gate":
        monkeypatch.setattr(S, "_gate_hold", lambda *a: True)
    if reason in ("ringer", "busy"):
        monkeypatch.setattr(S, f"_{reason}_hold", lambda *a: {"reason": "test"})
    if reason == "mute":
        monkeypatch.setattr(StateStore, "resolve_mute", lambda *a: True)
    sink = Sink()
    say(sink, lambda: reason != "toggle")
    assert sink.played == []
    assert StateStore().recent_history(sink="speech") == []


def test_live_thinking_is_not_synthesized_as_an_assistant_reply():
    state = StateStore()
    state.set_now_playing("speech", uri="fake", started_at=1, target="local",
                          extras={"source_session": "thinking-test", "thinking": True,
                                  "text": "A thought"})
    assert book_tracks._live_turn("thinking-test") is None


class CueSink(Sink):
    """Records earcons (play_cue) and clips in the order the player got them."""

    def play_cue(self, uri, target):
        self.played.append(("cue", Path(uri).name))
        return True

    def play_playlist(self, uris, target, **kw):
        self.played.extend(uris)
        return True


@pytest.fixture
def tones_on(monkeypatch):
    monkeypatch.delenv("MEDIA_EARCONS", raising=False)
    monkeypatch.setattr(S, "ringer_quiet", lambda *a: None)


def test_thinking_readout_opens_with_the_think_earcon(tones_on):
    from agent_media_core import earcons
    sink = CueSink()
    say(sink)
    assert sink.played[0] == ("cue", earcons.path("think").name)
    # Once, before the first clip; the sentences follow as before.
    assert [p for p in sink.played if isinstance(p, tuple)] == [sink.played[0]]
    assert len(sink.played) == 3


def test_think_earcon_has_its_own_switch(tones_on, monkeypatch):
    monkeypatch.setenv("MEDIA_EARCON_THINK", "0")
    sink = CueSink()
    say(sink)
    assert not any(isinstance(p, tuple) for p in sink.played)
    assert len(sink.played) == 2


class ReplyCoord(Coord):
    def pre_pause_remote(self): pass     # a reply may; thinking may not


def test_reply_does_not_open_with_the_think_earcon(tones_on, monkeypatch):
    monkeypatch.setattr(S, "_remote_playlist", lambda *a: False)
    sink = CueSink()
    S.submit_event(
        Event("A reply is long enough to be its own sentence. And a second one.",
              Source.CLAUDE_CODE, Priority.NORMAL, target=Target("local"),
              metadata={"session": "reply-test"}),
        state=StateStore(), sink=sink, coordinator=ReplyCoord())
    assert sink.played
    assert not any(isinstance(p, tuple) and "think" in p[1] for p in sink.played)


def test_think_earcon_is_short_and_quieter_than_the_cut_tick():
    import array
    from agent_media_core import earcons
    assert earcons.duration_s("think") < 0.4

    def peak(name):
        return max(abs(s) for s in array.array("h", earcons.synth(name)))
    assert peak("think") < peak("cut")
