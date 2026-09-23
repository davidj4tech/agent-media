"""Follow-along must survive the ways a reply loses sight of its player.

All three cases here end the same way in the wild: the reply keeps playing on
the phone and the thread stops bolding a sentence for the rest of it, because
the row that says "this is being spoken" is stale, missing a pause, or gone.

  * an incomplete snapshot is not an answer — `get_properties` leaves out the
    names that errored, and `.get("pause")` then reads an absent field as "not
    paused", which is how a reply the listener had paused was written off as
    stalled;
  * a reply that comes back from a barge-in *paused* still has to mark its row,
    or its clock runs on with the pause never taken off it;
  * the blind hold follows whatever snapshots do land, instead of waiting out
    the reply's length with the highlight frozen where it gave up.
"""

from pathlib import Path

import pytest

from agent_media_core.intake import submit as S
from agent_media_core.state import StateStore
from agent_media_core.types import Event, Source, Target


PHONE = Target(name="phone")
TEXT = "First sentence here. Second sentence here. Third sentence here."


@pytest.fixture(autouse=True)
def state_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(S.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setenv("MEDIA_SPEECH_SOCKET_PHONE", "tcp://127.0.0.1:6602")


def _fake_render(text, outfile, **_):
    Path(outfile).write_bytes(b"\x00")
    return True, ""


class _Coord:
    def pre_pause_remote(self): pass
    def before_speech(self, title="", priority="", defer_music=False, text=""): pass
    def speaking_line(self, text=""): pass
    def after_speech(self): pass
    def release_music_duck(self): pass
    def reapply_music_duck(self): pass


class _ScriptedSink:
    """Answers `snapshot` from a script (the last entry repeats)."""

    def __init__(self, snaps):
        self.snaps = list(snaps)
        self.playlists = 0
        self.jumps = []
        self.stops = 0

    def prefetch(self, paths, target=None): pass
    def play_playlist(self, uris, target=None, gapless=True): self.playlists += 1
    def set_playlist_pos(self, pos, target=None): self.jumps.append(pos)
    def stop(self, target=None): self.stops += 1
    def muted(self, target): return False
    def active_other_owner(self, target): return None
    def claim_broker(self, target): return True
    def refresh_broker(self, target): pass
    def release_broker(self, target): pass

    def snapshot(self, target=None):
        return self.snaps.pop(0) if len(self.snaps) > 1 else self.snaps[0]


def _run(sink, **kw):
    ev = Event(text=TEXT, source=Source.CLI, target=PHONE,
               metadata={"pane": "%7"})
    return S.submit_event(ev, state=StateStore(), sink=sink,
                          coordinator=_Coord(), **kw)


def _playing(pos, **extra):
    snap = {"playlist-pos": pos, "idle-active": False, "pause": False,
            "mute": False, "time-pos": 0.1 + pos}
    snap.update(extra)
    return snap


def test_a_snapshot_without_pause_is_not_read_as_playing(monkeypatch):
    """The stall guard must not fire on a reply that is merely paused.

    The phone answered without `pause` — the one field that says the silence is
    the listener's doing — so the loop saw time-pos standing still, called it
    a wedged clip after ~8s and ended the follow. The reply was still there,
    and the rest of it played with nothing bolded.
    """
    monkeypatch.setattr(S, "render_text", _fake_render)
    stalled = {"n": 0}
    real = S.log.warning

    def count(msg, *a, **k):
        if "stalled" in str(msg) or "no playback progress" in str(a):
            stalled["n"] += 1
        return real(msg, *a, **k)

    monkeypatch.setattr(S.log, "warning", count)
    # 200 ticks of a partial answer (no `pause`, frozen time-pos) — well past
    # both the ~5s miss bound and the ~8s stall bound — then a clean end.
    partial = {"playlist-pos": 0, "idle-active": False, "time-pos": 0.1}
    sink = _ScriptedSink([_playing(0)] + [partial] * 200 + [{"idle-active": True,
                                                            "pause": False}])
    _run(sink)
    assert stalled["n"] == 0


def test_a_reply_resumed_paused_still_marks_its_row(monkeypatch):
    """Back from a barge-in and paused: the row must say so.

    After a yield the highlight index is reset to -1 so the next reading
    re-shows the sentence. The pause branch marked off that same index, so a
    reply that came back paused marked nothing at all: `paused_at` was never
    stamped, `elapsed` kept running, and the app's bold left the voice behind
    for the rest of the reply.
    """
    monkeypatch.setattr(S, "render_text", _fake_render)
    seen = {"paused": 0}
    state = StateStore()
    real_set = StateStore.set_now_playing

    def spy(self, sink, **kw):
        ex = kw.get("extras") or {}
        if ex.get("live_pause"):
            seen["paused"] += 1
        return real_set(self, sink, **kw)

    monkeypatch.setattr(StateStore, "set_now_playing", spy)

    calls = {"n": 0}

    def fake_should_yield(self):
        calls["n"] += 1
        return calls["n"] == 3

    monkeypatch.setattr(S._SpeechPlaybackLock, "should_yield", fake_should_yield)
    monkeypatch.setattr(S._SpeechPlaybackLock, "yield_to_higher",
                        lambda self: None)

    # Reaches sentence 1, is interrupted, comes back paused for a few ticks,
    # then finishes.
    sink = _ScriptedSink([
        _playing(0), _playing(1),
        _playing(1, pause=True), _playing(1, pause=True), _playing(1, pause=True),
        {"idle-active": True, "pause": False},
    ])
    ev = Event(text=TEXT, source=Source.CLI, target=PHONE,
               metadata={"pane": "%7"})
    S.submit_event(ev, state=state, sink=sink, coordinator=_Coord())
    assert seen["paused"] >= 1


def test_the_blind_hold_keeps_following(monkeypatch):
    """Snapshots that come back during the hold move the highlight.

    The hold exists because the loop cannot see the player any more, but the
    reply is still audible. Waiting it out in silence meant the listener lost
    the bold for the rest of a reply over one unreadable stretch of link.
    """
    monkeypatch.setattr(S, "render_text", _fake_render)
    marked = []
    real_set = StateStore.set_now_playing

    def spy(self, sink, **kw):
        ex = kw.get("extras") or {}
        if ex.get("current_sentence_idx") is not None:
            marked.append(ex["current_sentence_idx"])
        return real_set(self, sink, **kw)

    monkeypatch.setattr(StateStore, "set_now_playing", spy)

    # Unreadable long enough to bail the loop (>50 misses), then the player
    # comes back on sentence 2 and finishes.
    sink = _ScriptedSink([_playing(0)] + [{}] * 60 + [_playing(2)] * 3
                         + [{"idle-active": True, "pause": False}])
    _run(sink)
    assert 2 in marked
