"""The duck waits for the sound on a lane that renders somewhere else.

`before_speech` returns when the reply has been handed over, and on the phone
lane that is when the *text* has been handed over — the phone renders its own
audio, which was measured at ten seconds on 2026-08-18. Ducking then opens a
hole in the music before anything fills it, which is what "it pauses a little
bit early" is.
"""

import threading

import pytest

from agent_media_core.route.coordinator import Coordinator
from agent_media_core.types import Target


class _FakeMusic:
    def __init__(self, uri="yt:track"):
        self._uri = uri
        self.calls = []

    def now_playing_uri(self, target=None): return self._uri
    def current_volume(self, target=None): return 130
    def position(self, target=None): return 1000
    def duck(self, target=None, level=15): self.calls.append(("duck", level))
    def unduck(self, target=None, restore=100): self.calls.append(("unduck", restore))
    def pause(self, target=None): self.calls.append(("pause",))
    def resume(self, target=None): self.calls.append(("resume",))


class _FakeBook:
    def active(self, target=None): return False
    def pause(self, target=None): pass
    def resume(self, target=None): pass
    def skip(self, seconds, target=None): pass


@pytest.fixture()
def coordinator(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_DUCK_ROOMS_STREAM", raising=False)
    # The flag and the card title are written straight into the *live* speech
    # broker — this machine's, and over the bridge the phone's. A test that
    # runs before_speech thirty times should not be a test that pokes whatever
    # is playing in the house, and watching a suite raise and lower the
    # speaking flag once a second on a real phone is how this was noticed.
    monkeypatch.setattr(Coordinator, "_speaking", lambda self, on: None)
    monkeypatch.setattr(Coordinator, "_title", lambda self, t: None)
    monkeypatch.setattr(Coordinator, "_priority", lambda self, p: None)
    music = _FakeMusic()
    c = Coordinator(music=music, book=_FakeBook(),
                    music_target=Target(name="phone"))
    return c, music


def test_deferring_decides_everything_but_the_moment(coordinator):
    c, music = coordinator
    c.before_speech(defer_music=True)
    assert music.calls == []          # nothing has happened to the music yet

    c.duck_music_now()
    assert [name for name, *_ in music.calls] == ["duck"]


def test_the_duck_is_applied_once_however_many_ask(coordinator):
    # Two callers race: the far side saying it is about to play, and the timer
    # that does not trust it to. Exactly one may duck — the second would
    # capture the *ducked* volume as the baseline to restore.
    c, music = coordinator
    c.before_speech(defer_music=True)

    done = threading.Barrier(4)

    def ask():
        done.wait()
        c.duck_music_now()

    threads = [threading.Thread(target=ask) for _ in range(3)]
    for t in threads:
        t.start()
    done.wait()
    for t in threads:
        t.join()
    assert [name for name, *_ in music.calls] == ["duck"]


def test_asking_without_a_deferral_does_nothing(coordinator):
    # Safe to call unconditionally: a lane that never deferred, a reply with no
    # music behind it, a second call after the first.
    c, music = coordinator
    c.duck_music_now()
    assert music.calls == []


def test_not_deferring_still_ducks_immediately(coordinator):
    # The local lane hands the clips to a player on this host and plays them a
    # moment later; nothing there is waiting on a render somewhere else.
    c, music = coordinator
    c.before_speech()
    assert [name for name, *_ in music.calls] == ["duck"]


def test_a_deferral_never_lands_on_the_next_reply(coordinator):
    # The utterance failed, or ended before anything asked. A duck applied
    # after that would have no speech behind it and nothing to restore it.
    c, music = coordinator
    c.before_speech(defer_music=True)
    c.after_speech()
    music.calls.clear()
    c.duck_music_now()
    assert music.calls == []


def test_restoring_after_a_deferred_duck_still_works(coordinator):
    c, music = coordinator
    c.before_speech(defer_music=True)
    c.duck_music_now()
    music.calls.clear()
    c.after_speech()
    assert [name for name, *_ in music.calls] == ["unduck"]
