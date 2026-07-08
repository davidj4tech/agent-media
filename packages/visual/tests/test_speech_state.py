"""The canvas's speech-state snapshot (drives motion + sound cues)."""

from agent_media_visual import canvas


def _fake_media(monkeypatch, line):
    monkeypatch.setattr(canvas, "_media", lambda args, timeout=10: line)


def test_playing_with_clock(monkeypatch):
    _fake_media(monkeypatch, "▶ 00:12 / 02:05")
    st = canvas.speech_state()
    assert st == {"kind": "state", "speaking": True, "pos": 12, "dur": 125}


def test_paused_is_not_speaking(monkeypatch):
    _fake_media(monkeypatch, "⏸ 00:12 / 02:05")
    st = canvas.speech_state()
    assert st["speaking"] is False
    assert st["pos"] == 12


def test_idle(monkeypatch):
    _fake_media(monkeypatch, "○")
    st = canvas.speech_state()
    assert st == {"kind": "state", "speaking": False}


def test_hour_long_clock(monkeypatch):
    _fake_media(monkeypatch, "▶ 1:02:03 / 1:10:00")
    st = canvas.speech_state()
    assert st["pos"] == 3723 and st["dur"] == 4200


def test_empty_output(monkeypatch):
    _fake_media(monkeypatch, "")
    st = canvas.speech_state()
    assert st == {"kind": "state", "speaking": False}


def test_state_events_do_not_clobber_last_image():
    hub = canvas.Hub()
    hub.publish({"image": "/img/x.webp"})
    hub.publish({"kind": "state", "speaking": True})
    assert hub.last == {"image": "/img/x.webp"}
    assert hub.last_state == {"kind": "state", "speaking": True}
