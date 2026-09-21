"""Interrupting music for speech decides which backend is live once.

Every router call re-resolved the live backend — a probe of the app, then of
the phone's mpv, each ~1s across the tailnet — and before_speech makes three
calls to interrupt music: what is playing, where it is, pause it. On 21 Sep
that was 2.7s to probe and ~3.5s more to pause, and nothing kept the three
answers from disagreeing.
"""

import threading

from agent_media_core.sinks.music_router import SinkMusicRouter
from agent_media_core.types import Target


class _Backend:
    def __init__(self, live):
        self.live = live
        self.loaded_asked = 0
        self.calls = []

    def loaded(self):
        self.loaded_asked += 1
        return self.live

    def now_playing_uri(self, target):
        self.calls.append("uri")
        return "file:///song.mp3"

    def position(self, target):
        self.calls.append("position")
        return 1000

    def pause(self, target):
        self.calls.append("pause")


def _router(monkeypatch):
    import agent_media_core.sinks.music_router as R
    monkeypatch.setattr(R, "_app_configured", lambda: True)
    monkeypatch.setattr(R, "_local_configured", lambda: True)
    app, local, mopidy = _Backend(False), _Backend(True), _Backend(False)
    return SinkMusicRouter(mopidy=mopidy, local=local, app=app), app, local


LOCAL = Target(name="local")


def _interrupt(r):
    r.now_playing_uri(LOCAL)
    r.position(LOCAL)
    r.pause(LOCAL)


def test_inside_the_block_the_backend_is_resolved_once(monkeypatch):
    r, app, local = _router(monkeypatch)
    with r.one_resolution():
        _interrupt(r)
    assert (app.loaded_asked, local.loaded_asked) == (1, 1)
    assert local.calls == ["uri", "position", "pause"]


def test_outside_it_every_call_still_resolves(monkeypatch):
    """Unchanged behaviour for everything else: playback moves between
    backends, and a long-lived caller must see it move."""
    r, app, local = _router(monkeypatch)
    _interrupt(r)
    assert (app.loaded_asked, local.loaded_asked) == (3, 3)


def test_the_answer_is_forgotten_when_the_block_ends(monkeypatch):
    r, app, local = _router(monkeypatch)
    with r.one_resolution():
        r.pause(LOCAL)
    local.live = False                      # the track ended
    r.pause(LOCAL)
    assert local.calls == ["pause"], "a stale answer outlived the block"


def test_concurrent_callers_share_one_answer(monkeypatch):
    """before_speech asks what is playing from a probe thread while its own
    thread carries on; both must get the same backend from one probe."""
    r, app, local = _router(monkeypatch)
    with r.one_resolution():
        ts = [threading.Thread(target=r.now_playing_uri, args=(LOCAL,))
              for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    assert local.loaded_asked == 1


def test_before_speech_asks_its_music_calls_to_agree(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from agent_media_core.route.coordinator import Coordinator
    from agent_media_core.state import StateStore

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    class _Music:
        pinned = False
        asked_pinned = []

        @contextmanager
        def one_resolution(self):
            _Music.pinned = True
            try:
                yield
            finally:
                _Music.pinned = False

        def now_playing_uri(self, target):
            _Music.asked_pinned.append(_Music.pinned)
            return None                      # nothing to interrupt

    class _Book:
        def active(self, target):
            return False

    Coordinator(music=_Music(), state=StateStore(), book=_Book()).before_speech()
    assert _Music.asked_pinned == [True], (
        "before_speech probed the music without pinning the backend")
