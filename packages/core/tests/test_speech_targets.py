"""Tests for 1C target-aware speech routing (sinks/speech)."""

import importlib

import pytest

from agent_media_core.sinks import speech
from agent_media_core.types import Target


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("MEDIA_SPEECH_") or k == "MEDIA_ROOMS_SINK":
            monkeypatch.delenv(k, raising=False)
    yield


def test_local_uses_broker_default():
    assert speech._device_for(Target("local")) is None


def test_rooms_defaults_to_pulse_am():
    assert speech._device_for(Target("rooms")) == "pulse/am"


def test_rooms_sink_name_override(monkeypatch):
    monkeypatch.setenv("MEDIA_ROOMS_SINK", "am-general")
    assert speech._device_for(Target("rooms")) == "pulse/am-general"


def test_explicit_device_override_wins(monkeypatch):
    monkeypatch.setenv("MEDIA_SPEECH_DEVICE_ROOMS", "pulse/elsewhere")
    assert speech._device_for(Target("rooms")) == "pulse/elsewhere"


def test_explicit_auto_means_default(monkeypatch):
    monkeypatch.setenv("MEDIA_SPEECH_DEVICE_KITCHEN", "auto")
    assert speech._device_for(Target("kitchen")) is None


def test_local_device_override(monkeypatch):
    monkeypatch.setenv("MEDIA_SPEECH_LOCAL_DEVICE", "pulse/am")
    assert speech._device_for(Target("local")) == "pulse/am"


def test_unknown_target_is_loud():
    with pytest.raises(NotImplementedError):
        speech._device_for(Target("kitchen"))


def test_socket_shared_by_default(monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg-state-test")
    s_local = speech._socket_for(Target("local"))
    s_rooms = speech._socket_for(Target("rooms"))
    assert s_local == s_rooms
    assert s_local.name == "sink-speech.sock"


def test_per_target_socket_override(monkeypatch):
    monkeypatch.setenv("MEDIA_SPEECH_SOCKET_ROOMS", "/run/rooms.sock")
    assert str(speech._socket_for(Target("rooms"))) == "/run/rooms.sock"


def test_play_sets_audio_device_then_loadfile(monkeypatch):
    calls = []
    monkeypatch.setattr(speech.ipc, "set_property",
                        lambda sock, name, value: calls.append(("set", name, value)))
    monkeypatch.setattr(speech.ipc, "command",
                        lambda sock, *args: calls.append(("cmd", *args)))
    speech.SinkSpeech().play("/tmp/x.mp3", Target("rooms"))
    assert calls[0] == ("set", "audio-device", "pulse/am")
    assert calls[1] == ("cmd", "loadfile", "/tmp/x.mp3", "replace")


def test_play_local_skips_device_set(monkeypatch):
    calls = []
    monkeypatch.setattr(speech.ipc, "set_property",
                        lambda sock, name, value: calls.append(("set", name, value)))
    monkeypatch.setattr(speech.ipc, "command",
                        lambda sock, *args: calls.append(("cmd", *args)))
    speech.SinkSpeech().play("/tmp/x.mp3", Target("local"))
    # local uses the broker's default device — no audio-device switch...
    assert not [c for c in calls if c[0] == "set" and c[1] == "audio-device"]
    # ...loadfile comes first...
    assert calls[0] == ("cmd", "loadfile", "/tmp/x.mp3", "replace")
    # ...and a fresh clip resets pause/mute so it's audible.
    assert ("set", "pause", False) in calls
    assert ("set", "mute", False) in calls
