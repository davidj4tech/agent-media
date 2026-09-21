"""load_playlist + start_playlist send what play_playlist always sent.

Split so a reply can load as soon as the broker is ours and start once the
music is paused (Sasonica fetches clips on append). The halves must add up to
the one batch exactly — in particular, loading must never start playback.
"""

import pytest

from agent_media_core.sinks import speech as SP
from agent_media_core.types import Target


APP = Target(name="app")


@pytest.fixture(autouse=True)
def _app_target(monkeypatch):
    # As red5 configures it.
    monkeypatch.setenv("MEDIA_SPEECH_SOCKET_APP", "tcp://phone.example:6613")
    monkeypatch.setenv("MEDIA_SPEECH_DEVICE_APP", "default")


@pytest.fixture
def sent(monkeypatch):
    batches = []
    monkeypatch.setattr(SP.ipc, "command_batch",
                        lambda sock, cmds, **k: batches.append(list(cmds)))
    return batches


def test_the_halves_add_up_to_the_whole(sent):
    s = SP.SinkSpeech()
    s.play_playlist(["/a.mp3", "/b.mp3"], APP)
    s.load_playlist(["/a.mp3", "/b.mp3"], APP)
    s.start_playlist(APP)
    whole, load, start = sent
    assert load + start == whole


def test_loading_never_starts_playback(sent):
    SP.SinkSpeech().load_playlist(["/a.mp3"], APP)
    (load,) = sent
    assert ["set_property", "playlist-pos", 0] not in load
    assert ["set_property", "pause", False] not in load
    assert all(c[-1] == "append" for c in load if c[0] == "loadfile")


def test_a_failed_load_reports_it(monkeypatch):
    def boom(*a, **k):
        raise SP.ipc.MpvIpcError("bridge down")

    monkeypatch.setattr(SP.ipc, "command_batch", boom)
    monkeypatch.setattr("agent_media_core.sinks._miss_notify.record_miss",
                        lambda *a, **k: None)
    assert SP.SinkSpeech().load_playlist(["/a.mp3"], APP) is False
