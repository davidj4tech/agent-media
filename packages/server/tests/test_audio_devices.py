"""Named frame players: isolated state, no real devices or playback."""

import pytest

from agent_media_core import audio_targets
from agent_media_server import audio_devices, devices, session_events, speech_frames as frames


@pytest.fixture(autouse=True)
def private_players(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(session_events, "poke", lambda: None)
    monkeypatch.setattr(devices, "list_devices", lambda: [
        {"id": "d_phone", "name": "David’s phone"}, {"id": "d_tv", "name": "Living-room TV"}])
    monkeypatch.setattr(audio_targets, "speech_configured", lambda t: (True, None))
    monkeypatch.setattr(audio_targets, "music_available", lambda t: (True, None))
    frames._reset_for_tests()
    yield
    frames._reset_for_tests()


def test_picked_phone_is_not_stolen_by_a_tv_connecting_or_reconnecting():
    phone = frames.listening("d_phone", True)
    frames.select_output("speech", "d_phone")
    tv = frames.listening("d_tv", True)
    assert frames._HUBS["speech"].device() == "d_phone"
    assert frames.playing_stream(phone) and not frames.playing_stream(tv)
    newer = frames.listening("d_phone", True)
    assert frames.playing_stream(newer) and not frames.playing_stream(phone)
    frames.listening("d_phone", False, newer)
    frames.listening("d_phone", False, phone)
    assert frames._HUBS["speech"].device() is None
    assert not frames.playing_stream(tv)  # An offline choice never becomes the TV.


def test_music_and_speech_choices_are_separate_and_persist():
    for ch in ("speech", "music"):
        frames.listening("d_phone", True, channel=ch)
        frames.listening("d_tv", True, channel=ch)
    audio_devices.select("music", "device:d_tv")
    audio_devices.select("speech", "device:d_phone")
    assert frames.output_device("music") == "d_tv"
    assert frames.output_device("speech") == "d_phone"
    frames._reset_for_tests()
    frames.listening("d_phone", True)
    frames.listening("d_tv", True)
    assert frames._HUBS["speech"].device() == "d_phone"


def test_current_audio_moves_with_a_targeted_stop_and_playhead_snapshot():
    phone = frames.listening("d_phone", True)
    frames.select_output("speech", "d_phone")
    tv = frames.listening("d_tv", True)
    hub = frames._HUBS["speech"]
    frames._send([{"op": "load", "uri": "https://example.test/one", "mode": "replace"},
                  {"op": "load", "uri": "https://example.test/two", "mode": "append"},
                  {"op": "pos", "i": 1}, {"op": "seek", "t": 8},
                  {"op": "pause", "on": True}], hub)
    old = frames.frames_after(0)[-1]
    frames.select_output("speech", "d_tv")
    stop, snapshot = frames.frames_after(old["seq"])
    assert stop["ops"] == [{"op": "stop"}]
    assert frames.playing_stream(phone, frame=stop) and not frames.playing_stream(tv, frame=stop)
    assert frames.playing_stream(tv, frame=snapshot) and not frames.playing_stream(phone, frame=snapshot)
    assert not frames.playing_stream(tv, frame=old)  # No stale phone clips on the TV.
    assert {"op": "pos", "i": 1} in snapshot["ops"]
    assert next(o["t"] for o in snapshot["ops"] if o["op"] == "seek") == pytest.approx(8, abs=0.1)
    assert {"op": "pause", "on": True} in snapshot["ops"]
    assert hub.model.pos == 1 and hub.model.entries == ["https://example.test/one", "https://example.test/two"]


def test_picker_shows_named_devices_and_offline_choice():
    phone = frames.listening("d_phone", True)
    frames.select_output("speech", "d_phone")
    frames.listening("d_phone", False, phone)
    frames.listening("d_tv", True)
    block = audio_devices.block("speech", {"current": "sasonica", "options": []})
    assert block["current"] == "device:d_phone" and block["overridden"]
    rows = {r["name"]: r for r in block["options"]}
    assert rows["device:d_phone"]["label"] == "David’s phone"
    assert not rows["device:d_phone"]["available"]
    assert rows["device:d_tv"]["available"]
    assert rows["device:d_tv"]["label"] == "Living-room TV"


def test_only_a_paired_connected_device_can_be_selected():
    with pytest.raises(ValueError, match="unknown paired"):
        audio_devices.select("speech", "device:d_unknown")
    with pytest.raises(ValueError, match="not connected"):
        audio_devices.select("speech", "device:d_tv")
    assert frames.output_device("speech") is None
