"""A muted pane renders + records history but never plays or ducks.

Covers both intake entry points (submit_event and submit_stream).
"""

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
    monkeypatch.delenv("MEDIA_SPEECH_DEFAULT_TARGET", raising=False)
    return tmp_path


def _fake_render(text, outfile, **_):
    Path(outfile).write_bytes(b"\x00")
    return True, ""


class _RecordingCoord:
    """Records whether the playback-coordination hooks were touched."""

    def __init__(self):
        self.pre_pause = 0
        self.before = 0
        self.after = 0

    def pre_pause_remote(self):
        self.pre_pause += 1

    def before_speech(self, title=""):
        self.before += 1

    def after_speech(self):
        self.after += 1


class _LoudSink:
    """Any play() here is a failure: a muted pane must never reach the broker."""

    def __init__(self):
        self.plays = 0

    def play(self, uri, target, **_):
        self.plays += 1

    def prefetch(self, paths, target=None):
        pass

    def play_playlist(self, uris, target=None, gapless=True):
        self.plays += len(list(uris))

    def playlist_pos(self, target=None):
        return None

    def set_playlist_pos(self, pos, target=None):
        pass

    def idle(self, target):
        return True

    def paused(self, target):
        return False

    def muted(self, target):
        return False


def _muted_event():
    return Event(text="First sentence here. Second sentence here.",
                 source=Source.CLI, metadata={"pane": "%7"})


def test_submit_event_muted_renders_records_but_no_playback(state_env, monkeypatch):
    monkeypatch.setattr(S, "render_text", _fake_render)
    state = StateStore()
    state.set_mute("pane", "%7", True)
    coord, sink = _RecordingCoord(), _LoudSink()

    rid = S.submit_event(_muted_event(), state=state, sink=sink, coordinator=coord)

    # History row exists and is flagged muted, with the rendered clips attached.
    assert rid is not None
    row = state.recent_history(sink="speech", limit=1)[0]
    assert row["extras"]["muted"] is True
    clips = row["extras"]["clip_uris"]
    assert clips and all(Path(c).exists() for c in clips)

    # Nothing played, nothing ducked, no now_playing left behind.
    assert sink.plays == 0
    assert (coord.pre_pause, coord.before, coord.after) == (0, 0, 0)
    assert state.get_now_playing("speech") is None


def test_submit_stream_muted_renders_records_but_no_playback(state_env, monkeypatch):
    monkeypatch.setattr(S, "render_text", _fake_render)
    state = StateStore()
    state.set_mute("pane", "%7", True)
    coord, sink = _RecordingCoord(), _LoudSink()

    rid = S.submit_stream(iter(["First sentence here.", "Second one here."]),
                          _muted_event(), state=state, sink=sink, coordinator=coord)

    assert rid is not None
    row = state.recent_history(sink="speech", limit=1)[0]
    assert row["extras"]["muted"] is True
    clips = row["extras"]["clip_uris"]
    assert clips and all(Path(c).exists() for c in clips)
    assert sink.plays == 0
    assert (coord.pre_pause, coord.before, coord.after) == (0, 0, 0)
    assert state.get_now_playing("speech") is None


def test_unmuted_pane_still_plays(state_env, monkeypatch):
    """Control: with no mute set, the same event plays normally."""
    monkeypatch.setattr(S, "render_text", _fake_render)
    state = StateStore()  # no mute row for %7
    coord = _RecordingCoord()

    class _PlaySink(_LoudSink):
        def idle(self, target):
            # False on the very first probe (playback started), True after, so
            # each clip's wait finishes promptly. Mirrors the sidecar test.
            self.calls = getattr(self, "calls", 0) + 1
            return self.calls > 1

    sink = _PlaySink()
    rid = S.submit_event(_muted_event(), state=state, sink=sink, coordinator=coord)

    assert rid is not None
    assert sink.plays >= 1
    assert coord.before == 1
    assert "muted" not in (state.recent_history(sink="speech", limit=1)[0]["extras"])
