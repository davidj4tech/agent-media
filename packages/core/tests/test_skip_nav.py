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
        def before_speech(self, title="", priority="", defer_music=False,
                          text=""): pass
        def speaking_line(self, text=""): pass
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


# --- _HighlightScheduler: deferred highlight aligned to Snapcast buffer ---


def _patch_highlight(monkeypatch):
    """Record (sentence, first, force, t) for every highlight that fires."""
    calls = []
    start = time.monotonic()

    def _rec(text, *, first=False, force=False):
        calls.append((text, first, force, time.monotonic() - start))

    monkeypatch.setattr(S, "_tmux_highlight_text", _rec)
    # The scheduler asks per sentence whether a force-highlight press is in
    # effect (that is how turning follow-along on mid-reply revives a skipped
    # turn), and that flag is a real file in the developer's own state dir —
    # so without this the suite's answer depends on whether they last pressed
    # `v`. These tests are about the scheduler, not the ambient flag.
    monkeypatch.setattr(S, "_force_highlight_active", lambda pane: False)
    # The scheduler also publishes the rows' text to tmux on every sentence.
    # That is a real subprocess against the developer's own server — which both
    # slows the "fires synchronously" timing below past its threshold and
    # writes options into the session they are watching.
    monkeypatch.setattr(S, "publish_follow_text", lambda *a, **k: None)
    return calls


def test_scheduler_disabled_is_noop(monkeypatch):
    calls = _patch_highlight(monkeypatch)
    h = S._HighlightScheduler(0.05, enabled=False)
    h.show("hi", first=True, force=False)
    h.drain()
    assert calls == []


def test_scheduler_zero_delay_fires_sync(monkeypatch):
    calls = _patch_highlight(monkeypatch)
    h = S._HighlightScheduler(0.0, enabled=True)
    h.show("hi", first=True, force=False)
    assert [c[0] for c in calls] == ["hi"]  # already fired, no drain needed
    assert calls[0][3] < 0.02


def test_scheduler_defers_until_drain(monkeypatch):
    calls = _patch_highlight(monkeypatch)
    h = S._HighlightScheduler(0.1, enabled=True)
    h.show("later", first=False, force=False)
    assert calls == []  # not yet — buffer delay not elapsed
    h.drain()
    assert [c[0] for c in calls] == ["later"]
    assert calls[0][3] >= 0.1


def test_scheduler_queues_short_clips_in_order(monkeypatch):
    """Back-to-back clips shorter than the delay must all fire, in order,
    not cancel one another."""
    calls = _patch_highlight(monkeypatch)
    h = S._HighlightScheduler(0.15, enabled=True)
    h.show("one", first=True, force=False)
    time.sleep(0.02)
    h.show("two", first=False, force=False)
    time.sleep(0.02)
    h.show("three", first=False, force=False)
    h.drain()
    assert [c[0] for c in calls] == ["one", "two", "three"]


def test_scheduler_force_fires_now_and_drops_queue(monkeypatch):
    calls = _patch_highlight(monkeypatch)
    h = S._HighlightScheduler(0.2, enabled=True)
    h.show("queued", first=False, force=False)  # pending
    h.show("jumped", first=False, force=True)   # manual skip
    # forced one is immediate...
    assert [c[0] for c in calls] == ["jumped"]
    assert calls[0][2] is True  # force flag passed through
    h.drain()
    # ...and the queued highlight was cancelled, never fires
    assert [c[0] for c in calls] == ["jumped"]


def test_scheduler_cancel_pending_drops_tail(monkeypatch):
    calls = _patch_highlight(monkeypatch)
    h = S._HighlightScheduler(0.2, enabled=True)
    h.show("a", first=False, force=False)
    h.show("b", first=False, force=False)
    h.cancel_pending()
    h.drain()
    assert calls == []
