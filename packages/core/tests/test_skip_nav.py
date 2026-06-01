"""Live-path nav: the reader loop's _wait_for_clip honors a popup skip flag."""

import threading
import time
from pathlib import Path

from agent_media_core.intake import submit as S
from agent_media_core.state import StateStore
from agent_media_core.types import Event, Source, Target


class _IdleSink:
    """idle() flips to True after the first call; never paused."""
    def __init__(self):
        self.n = 0

    def idle(self, target):
        self.n += 1
        return self.n > 1

    def paused(self, target):
        return False


def test_nav_flag_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    t = Target(name="rooms")
    assert S._read_nav_request(t) is None
    S._nav_flag_path(t).write_text("3")
    assert S._read_nav_request(t) == 3
    # consumed on read
    assert S._read_nav_request(t) is None


def test_wait_for_clip_returns_nav_index(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    t = Target(name="rooms")
    S._nav_flag_path(t).write_text("2")

    class _Playing:
        def idle(self, target):
            return False  # never idle — only the nav flag should end the wait

        def paused(self, target):
            return False

    assert S._wait_for_clip(_Playing(), t) == 2
    assert S._read_nav_request(t) is None  # flag consumed


def test_wait_for_clip_natural_end_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    t = Target(name="rooms")
    assert S._wait_for_clip(_IdleSink(), t) is None


def test_wait_for_clip_nav_honored_while_paused(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    t = Target(name="rooms")
    S._nav_flag_path(t).write_text("1")

    class _Paused:
        def idle(self, target):
            return False

        def paused(self, target):
            return True  # paused the whole time; nav must still return

    assert S._wait_for_clip(_Paused(), t) == 1


def test_reader_loop_honors_midresponse_sentence_jump(tmp_path, monkeypatch):
    """End-to-end: inject a nav request mid-readout and the loop jumps clips.

    Drives the real submit_event reader loop with a controllable fake sink
    (no audio): clip 0 plays, a background thread requests a jump to clip 2,
    and we assert the loop skipped clip 1 and landed on clip 2.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MEDIA_RENDER_ENGINE", "edge")
    monkeypatch.setenv("MEDIA_RENDER_VOICE", "en-US-AriaNeural")
    monkeypatch.setenv("MEDIA_SPEECH_DEFAULT_TARGET", "rooms")
    monkeypatch.setenv("MEDIA_SNAPCAST_LATENCY_MS", "0")

    def _fake_render(text, outfile, **_):
        Path(outfile).write_bytes(b"\x00")
        return True, ""

    monkeypatch.setattr(S, "render_text", _fake_render)
    state = StateStore()

    text = ("Sentence one is plenty long. "
            "Sentence two is also long. "
            "Sentence three is the last.")
    target = Target(name="rooms")

    class _Sink:
        def __init__(self):
            self.played = []
            self.allow_idle = False

        def play(self, uri, target, **kw):
            self.played.append(uri)

        def idle(self, target):
            return self.allow_idle

        def paused(self, target):
            return False

    sink = _Sink()

    class _Coord:
        def pre_pause_remote(self): pass
        def before_speech(self): pass
        def after_speech(self): pass

    def _injector():
        # Wait until clip 0 is playing, then request a jump to clip index 2.
        for _ in range(100):
            if sink.played:
                break
            time.sleep(0.02)
        S._nav_flag_path(target).write_text("2")
        # After the loop lands on clip 2, let the clip "finish" so playback ends.
        for _ in range(100):
            if len(sink.played) >= 2:
                break
            time.sleep(0.02)
        sink.allow_idle = True

    th = threading.Thread(target=_injector)
    th.start()
    S.submit_event(Event(text=text, source=Source.CLI),
                   state=state, sink=sink, coordinator=_Coord())
    th.join(timeout=5)

    # Clip 1 was skipped: the second thing played is sentence index 2 (003),
    # never 002.
    assert len(sink.played) == 2
    assert sink.played[0].endswith("000.mp3")
    assert sink.played[1].endswith("002.mp3")
    assert not any(p.endswith("001.mp3") for p in sink.played)
