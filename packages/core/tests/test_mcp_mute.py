"""MCP tools for durable per-pane / per-session mute."""

import pytest

from agent_media_core import mcp_server as m
from agent_media_core.state import StateStore


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # _state() memoizes a StateStore on the function; drop it so it re-reads
    # this test's XDG_STATE_HOME, and assert against that same instance.
    if hasattr(m._state, "_v"):
        del m._state._v
    return m._state()


def test_mute_pane_explicit(env):
    out = m.mute_pane(pane="%5", state="on")
    assert out["ok"] is True and out["scope"] == "pane"
    assert out["key"] == "%5" and out["muted"] is True
    assert env.get_mute("pane", "%5") is True
    assert m.mute_pane(pane="%5", state="toggle")["muted"] is False
    assert env.get_mute("pane", "%5") is False


def test_mute_pane_stops_in_flight_clip(env, monkeypatch):
    # Active speech from %5; muting it should stop the broker immediately.
    env.set_now_playing("speech", uri="/x.mp3", started_at=1.0,
                        extras={"source_pane": "%5",
                                "source_tmux_session": "work:"})
    stops = []
    monkeypatch.setattr(m, "_speech",
                        lambda: type("S", (), {"stop": lambda self, t: stops.append(t)})())
    out = m.mute_pane(pane="%5", state="on")
    assert out["stopped_current"] is True
    assert stops  # broker.stop was called

    # Muting a *different* pane leaves the current clip alone.
    stops.clear()
    out = m.mute_pane(pane="%6", state="on")
    assert out["stopped_current"] is False
    assert not stops


def test_mute_pane_session(env):
    m.mute_pane(session="work:", state="on")
    assert env.get_mute("session", "work:") is True


def test_mute_pane_defaults_to_last_speaker(env, monkeypatch):
    monkeypatch.setattr(m, "_last_speaking_pane", lambda: "%8")
    assert m.mute_pane(state="on")["key"] == "%8"
    assert env.get_mute("pane", "%8") is True


def test_mute_pane_no_target(env, monkeypatch):
    monkeypatch.setattr(m, "_last_speaking_pane", lambda: "")
    out = m.mute_pane()
    assert out["ok"] is False


def test_mute_status_lists(env):
    env.set_mute("pane", "%1", True)
    env.set_mute("session", "work:", False)
    assert m.mute_status() == {
        "panes": {"%1": True}, "sessions": {"work:": False}}
