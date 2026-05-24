"""Tests for the spoken-text sidecar + now-speaking record (task #8)."""

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


class _FakeSink:
    def __init__(self):
        self.calls = 0
        self.played = None

    def play(self, uri, target, **_):
        self.played = uri

    def idle(self, target):
        # call1 -> False (playing started), call2 -> True (finished)
        self.calls += 1
        return self.calls > 1


def test_sidecar_and_now_playing_lifecycle(state_env, monkeypatch):
    monkeypatch.setattr(S, "render_text", _fake_render)
    state = StateStore()

    during = {}

    class _Coord:
        def before_speech(self):
            during["np"] = state.get_now_playing("speech")

        def after_speech(self):
            pass

    sink = _FakeSink()
    rid = S.submit_event(Event(text="hello world", source=Source.CLI),
                         state=state, sink=sink, coordinator=_Coord())

    # history row written
    assert rid is not None
    clip = Path(sink.played)

    # 1. text sidecar sits next to the clip with the spoken text
    sidecar = clip.with_suffix(".txt")
    assert sidecar.exists()
    assert sidecar.read_text() == "hello world"

    # 2. now-speaking was live *during* playback, carrying the text
    assert during["np"] is not None
    extras = during["np"].get("extras") or {}
    if isinstance(extras, str):
        import json
        extras = json.loads(extras)
    assert extras.get("text") == "hello world"
    assert during["np"]["uri"] == str(clip)

    # 3. now-speaking cleared after playback
    assert state.get_now_playing("speech") is None


def test_empty_text_no_sidecar(state_env, monkeypatch):
    monkeypatch.setattr(S, "render_text", _fake_render)
    state = StateStore()
    rid = S.submit_event(Event(text="   ", source=Source.CLI),
                         state=state, sink=_FakeSink())
    assert rid is None  # nothing rendered for empty text
