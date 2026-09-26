"""The broker claim runs beside before_speech, not ahead of it.

Both are strings of round trips to the phone — the claim to the speech player,
before_speech to the music and book players — and over a 430ms link they cost
5.8s and 12.8s back to back (21 Sep). Overlapping them is only safe if the
claim still lands before anything is fed to the broker, and is never released
before it has finished.
"""

import threading

from pathlib import Path

import pytest

from agent_media_core.intake import submit as S
from agent_media_core.state import StateStore
from agent_media_core.types import Event, Source, Target


PHONE = Target(name="phone")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("MEDIA_SPEECH_SOCKET_PHONE", "tcp://127.0.0.1:6602")
    monkeypatch.setenv("MEDIA_RENDER_ENGINE", "edge")
    monkeypatch.setenv("MEDIA_RENDER_VOICE", "en-US-AriaNeural")
    monkeypatch.delenv("MEDIA_STREAM_CLIPS", raising=False)
    monkeypatch.setattr(S.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(S, "_clip_duration", lambda _p: 2.0)
    monkeypatch.setattr(S, "render_text", _render)


def _render(text, outfile, **_):
    Path(outfile).write_bytes(b"\x00")
    return True, ""


class _Coord:
    def __init__(self, started, fail=False):
        self._started = started
        self._fail = fail

    def pre_pause_remote(self):
        pass

    def before_speech(self, title="", priority="", defer_music=False, text=""):
        self._started.set()
        if self._fail:
            raise RuntimeError("the music player went away")

    def speaking_line(self, text=""):
        pass

    def after_speech(self):
        pass

    def release_music_duck(self):
        pass

    def reapply_music_duck(self):
        pass


class _Sink:
    """Its claim only completes once before_speech has begun — which it can
    only do if the two are running at the same time."""

    def __init__(self, started):
        self._started = started
        self.saw_before_speech = False
        self.events = []

    def active_other_owner(self, target):
        return None

    def claim_broker(self, target):
        self.saw_before_speech = self._started.wait(timeout=5)
        self.events.append("claimed")
        return True

    def release_broker(self, target):
        self.events.append("released")

    def refresh_broker(self, target):
        pass

    def prefetch(self, paths, target=None):
        self.events.append("prefetched")

    def play_playlist(self, uris, target=None, gapless=True):
        self.events.append("played")

    def snapshot(self, target=None):
        return {"idle-active": True}

    def set_playlist_pos(self, pos, target=None):
        pass

    def stop(self, target=None):
        pass

    def muted(self, target):
        return False


def _say(sink, coord):
    return S.submit_event(
        Event(text="A reply long enough to be spoken on its own here.",
              source=Source.CLI, target=PHONE, metadata={"pane": "%7"}),
        state=StateStore(), sink=sink, coordinator=coord)


def test_the_claim_runs_while_before_speech_does():
    started = threading.Event()
    sink = _Sink(started)
    _say(sink, _Coord(started))

    assert sink.saw_before_speech, (
        "the claim finished before before_speech began — they ran in series")


def test_nothing_is_played_before_the_claim_lands():
    started = threading.Event()
    sink = _Sink(started)
    _say(sink, _Coord(started))

    assert sink.events.index("claimed") < sink.events.index("played")
    assert sink.events.index("prefetched") < sink.events.index("played")


def test_a_music_player_failing_does_not_stop_the_reply():
    """before_speech runs beside the start (27 Sep 2026), so a music player
    that fails costs the pause, not the reply — and the claim still lands
    before the release, or it would hold the broker for its TTL."""
    started = threading.Event()
    sink = _Sink(started)
    _say(sink, _Coord(started, fail=True))

    assert "played" in sink.events
    assert sink.events.index("claimed") < sink.events.index("released")


# --- streaming: the render hides behind the pre-speech round trips --------


class _CoordAfter(_Coord):
    def __init__(self, started, fail=False):
        super().__init__(started, fail)
        self.after = 0

    def after_speech(self):
        self.after += 1


def _streaming(monkeypatch):
    monkeypatch.setenv("MEDIA_STREAM_CLIPS", "1")
    monkeypatch.setenv("MEDIA_STREAM_LEAD_S", "2")


def test_the_lead_renders_behind_before_speech_not_ahead_of_it(monkeypatch):
    """The first sentence used to be waited for before the token was even
    taken — 1.3s in series. Its render now only has to be done by the time
    before_speech is: this render cannot finish until before_speech begins."""
    _streaming(monkeypatch)
    started = threading.Event()
    seen = {}

    def render(text, outfile, **_):
        seen["before_speech_had_begun"] = started.wait(timeout=5)
        return _render(text, outfile)

    monkeypatch.setattr(S, "render_text", render)
    sink = _Sink(started)
    _say(sink, _Coord(started))

    assert seen["before_speech_had_begun"], (
        "the reply waited for its lead before before_speech could start")
    assert "played" in sink.events


def test_a_reply_that_renders_nothing_lets_go_of_everything(monkeypatch):
    """Found out while holding the token now, so it leaves by the same door
    as any other reply: music restored, broker and token released."""
    _streaming(monkeypatch)
    monkeypatch.setattr(S, "render_text", lambda *a, **k: (False, "engine down"))
    started = threading.Event()
    sink, coord = _Sink(started), _CoordAfter(started)

    assert _say(sink, coord) is None
    assert "played" not in sink.events
    assert coord.after == 1
    assert sink.events.index("claimed") < sink.events.index("released")


def test_before_speech_failing_cannot_strand_the_claim_thread(monkeypatch):
    """The claim thread's prefetch waits for the lead. If before_speech raises,
    nobody collects it — the way out must release that wait, not join a
    thread that will never finish."""
    _streaming(monkeypatch)
    started = threading.Event()
    sink = _Sink(started)
    raised = []

    def run():
        try:
            _say(sink, _Coord(started, fail=True))
        except RuntimeError as e:
            raised.append(e)

    # Run it aside, so the bug this guards against fails the test instead of
    # hanging the whole run.
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "the reply deadlocked joining the claim thread"
    assert not raised, "a music player failing is logged, not the reply's end"
    assert sink.events.index("claimed") < sink.events.index("released")


# --- load early, start late -------------------------------------------------


class _SplitSink(_Sink):
    """A player that can be loaded and started apart, like SinkSpeech."""

    def __init__(self, started, load_ok=True):
        super().__init__(started)
        self.loaded = threading.Event()
        self._load_ok = load_ok

    def load_playlist(self, uris, target=None, gapless=True):
        self.events.append("loaded")
        self.loaded.set()
        return self._load_ok

    def start_playlist(self, target=None):
        self.events.append("started")
        return True


class _SlowCoord(_Coord):
    """before_speech that takes as long as pausing music does — long enough
    to find out whether the playlist was loaded while it ran."""

    def __init__(self, started, sink):
        super().__init__(started)
        self._sink = sink
        self.loaded_during = None

    def before_speech(self, title="", priority="", defer_music=False, text=""):
        self._started.set()
        self.loaded_during = self._sink.loaded.wait(timeout=5)


def test_the_playlist_is_loaded_while_before_speech_runs():
    """Sasonica fetches a clip as it is appended. Loading once the broker is
    ours — not once the music is paused — lets that fetch run behind it."""
    started = threading.Event()
    sink = _SplitSink(started)
    coord = _SlowCoord(started, sink)
    _say(sink, coord)

    assert coord.loaded_during, "the playlist was only loaded after before_speech"
    assert sink.events.index("claimed") < sink.events.index("loaded")
    assert sink.events.index("prefetched") < sink.events.index("loaded")
    assert sink.events.index("loaded") < sink.events.index("started")
    assert "played" not in sink.events, "loaded AND played in one batch"


def test_a_load_that_failed_falls_back_to_playing_it_whole():
    started = threading.Event()
    sink = _SplitSink(started, load_ok=False)
    _say(sink, _Coord(started))

    assert "played" in sink.events
    assert "started" not in sink.events


def test_a_streamed_reply_that_renders_nothing_loads_nothing(monkeypatch):
    _streaming(monkeypatch)
    monkeypatch.setattr(S, "render_text", lambda *a, **k: (False, "engine down"))
    started = threading.Event()
    sink = _SplitSink(started)
    _say(sink, _Coord(started))

    assert "loaded" not in sink.events
    assert "started" not in sink.events
