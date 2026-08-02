"""Tests for the sticky speech rate.

Speed lives on the long-lived broker mpv, so a 1.5× set mid-reply still applies
to the next one. The store keeps a copy purely so the popup can *say so* while
idle — the remote (phone) path has no now_playing mirror to read once playback
stops, and a bridge round-trip per redraw is too expensive to pay.
"""

from agent_media_core import cli
from agent_media_core.state import StateStore


def _store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "state.db")


def test_roundtrips_a_rate(tmp_path):
    st = _store(tmp_path)
    assert st.get_speech_speed() is None
    st.set_speech_speed(1.5)
    assert st.get_speech_speed() == 1.5


def test_normal_rate_clears_the_row(tmp_path):
    st = _store(tmp_path)
    st.set_speech_speed(1.5)
    st.set_speech_speed(1.0)
    assert st.get_speech_speed() is None
    st.set_speech_speed(1.5)
    st.set_speech_speed(None)
    assert st.get_speech_speed() is None


def test_sticky_falls_back_to_the_stored_rate_when_idle(monkeypatch, tmp_path):
    st = _store(tmp_path)
    st.set_speech_speed(0.8)
    monkeypatch.setattr(cli, "StateStore", lambda: st)
    assert cli._sticky_speech_speed(None) == 0.8


def test_sticky_prefers_and_records_a_live_reading(monkeypatch, tmp_path):
    st = _store(tmp_path)
    st.set_speech_speed(0.8)
    monkeypatch.setattr(cli, "StateStore", lambda: st)
    assert cli._sticky_speech_speed(1.25) == 1.25
    assert st.get_speech_speed() == 1.25
    # Back to normal: the live reading wins and the row goes away, so a broker
    # that restarted at 1.0 stops being reported as fast.
    assert cli._sticky_speech_speed(1.0) == 1.0
    assert st.get_speech_speed() is None
