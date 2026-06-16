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
    assert m.mute_pane(pane="%5", state="on") == {
        "ok": True, "scope": "pane", "key": "%5", "muted": True}
    assert env.get_mute("pane", "%5") is True
    assert m.mute_pane(pane="%5", state="toggle")["muted"] is False
    assert env.get_mute("pane", "%5") is False


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
