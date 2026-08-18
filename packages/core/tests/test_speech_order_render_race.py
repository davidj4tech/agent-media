"""Same-session replies are heard in the order they were submitted, even when
a later one renders faster.

The regression: rendering runs outside the playback lock (deliberately — that's
what lets sessions render in parallel), and TTS render time scales with the
text. A long reply submitted first therefore reached the lock *after* the short
follow-up submitted seconds later, and the pair came out of the speaker back to
front — the short reply first, then the long one it was answering after.
"""

import threading
import time
from pathlib import Path

import pytest

from agent_media_core.intake import submit as S
from agent_media_core.state import StateStore
from agent_media_core.types import Event, Source


@pytest.fixture
def state_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("MEDIA_RENDER_ENGINE", "edge")
    monkeypatch.setenv("MEDIA_RENDER_VOICE", "en-US-AriaNeural")
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)
    monkeypatch.delenv("MEDIA_SPEECH_DEFAULT_TARGET", raising=False)
    monkeypatch.setenv("MEDIA_SPEECH_LOCK_TIMEOUT_S", "10")
    return tmp_path


class _Coord:
    def pre_pause_remote(self):
        pass

    def before_speech(self, title="", priority="", defer_music=False,
                          text=""):
        pass

    def speaking_line(self, text=""): pass
    def after_speech(self):
        pass


class _OrderSink:
    """Records the order in which whole replies reach the broker."""

    def __init__(self):
        self.order: list[str] = []
        self._lock = threading.Lock()
        self._idle: dict[str, bool] = {}

    def play(self, uri, target, **_):
        with self._lock:
            self.order.append(Path(uri).name)
            self._idle[uri] = False

    def idle(self, target):
        return True     # each clip finishes as soon as it starts

    def paused(self, target):
        return False


def test_long_reply_speaks_before_the_short_one_submitted_after_it(
        state_env, monkeypatch):
    # Render time proportional to the text, as a real TTS engine's is: the long
    # reply takes 1.2s per sentence, the short one 0.05s.
    def _slow_render(text, outfile, **_):
        time.sleep(1.2 if len(text) > 40 else 0.05)
        Path(outfile).write_bytes(b"\x00")
        return True, ""

    monkeypatch.setattr(S, "render_text", _slow_render)
    monkeypatch.setattr(S, "_clip_duration", lambda p: 0.0)
    sink = _OrderSink()
    state = StateStore()

    long_text = ("This is the long reply that takes a while to render. "
                 "It has a second sentence as well, also rather long.")
    short_text = "No worries."

    def speak(text):
        S.submit_event(
            Event(text=text, source=Source.CLAUDE_CODE,
                  metadata={"session": "sess-1"}),
            state=state, sink=sink, coordinator=_Coord())

    t_long = threading.Thread(target=speak, args=(long_text,))
    t_long.start()
    time.sleep(0.2)                      # the follow-up lands a beat later...
    t_short = threading.Thread(target=speak, args=(short_text,))
    t_short.start()
    t_long.join(timeout=30)
    t_short.join(timeout=30)

    # ...but is still heard second: three clips, the long reply's two first.
    assert len(sink.order) == 3
    assert sink.order[2].endswith("000.mp3")          # the short reply, last
    assert sink.order[0][:15] == sink.order[1][:15]   # same submission stamp
    rows = state.recent_history(sink="speech", limit=2)
    assert rows[0]["text"] == short_text              # newest row = short reply
    assert rows[1]["text"] == long_text
