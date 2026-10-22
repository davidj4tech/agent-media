"""Voice mode's quick model (voice_model.py, voice_mode.py): a thread in the
hands-free loop talks on a faster model and goes back to its own after, only
between turns, without touching the user's default for new sessions."""

from __future__ import annotations

import json

import pytest

from agent_media_core import voice_mode
from agent_media_core.intake import heard
from agent_media_server import driver, session_settings, sessions, voice_model

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture()
def world(monkeypatch, tmp_path):
    """A pane thread on opus, between turns, and a settings file whose
    `model` Claude Code rewrites on every typed `/model` (as 2.1.308 does)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.delenv("MEDIA_VOICE_MODEL", raising=False)
    monkeypatch.delenv("MEDIA_VOICE_MODEL_CLAUDE", raising=False)
    monkeypatch.delenv("MEDIA_VOICE_SUBAGENT_MODEL_CLAUDE", raising=False)
    (tmp_path / "claude").mkdir()
    settings = tmp_path / "claude" / "settings.json"
    settings.write_text(json.dumps({"model": "opus", "hooks": {"Stop": []}}, indent=2))
    w = {"agent": "claude", "model": "opus", "idle": True, "told": [],
         "driver": driver.PANE, "settings": settings}

    def configure_pane(session, *, model=None, mode=None):
        w["told"].append(model)
        if not w["idle"]:
            return False, {"error": "wait for the turn to finish", "status": 409}
        w["model"] = model
        data = json.loads(settings.read_text())
        data["model"] = model  # what the TUI does: saved as the default
        settings.write_text(json.dumps(data, indent=2))
        return True, {"session": session, "told": True, "pane": "%42"}

    monkeypatch.setattr(session_settings, "configure_pane", configure_pane)
    monkeypatch.setattr(voice_model, "_current",
                        lambda s: (w["agent"], w["model"] if w["driver"] == driver.PANE
                                   else w.get("kept", ""), w["model"]))
    monkeypatch.setattr(voice_model, "_idle", lambda s: (w["idle"], w["driver"]))
    monkeypatch.setattr(voice_model, "_answered", lambda pane, model: True)
    monkeypatch.setattr(voice_model, "_SAVE_S", 0.0)
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%42"})
    monkeypatch.setattr(driver, "owned_headless", lambda s: w["driver"] == driver.HEADLESS)
    return w


def _default(w):
    return json.loads(w["settings"].read_text()).get("model")


def test_voice_switches_a_pane_to_sonnet_and_back(world):
    voice_mode.mark(SID, True)
    voice_model.on_reply(SID, True)
    assert world["told"] == ["sonnet"] and world["model"] == "sonnet"
    assert voice_mode.switched(SID)["prev"] == "opus"
    # The user's default for new sessions is left as it was.
    assert _default(world) == "opus"
    assert json.loads(world["settings"].read_text())["hooks"] == {"Stop": []}
    # A second voice reply does not type it again.
    voice_model.on_reply(SID, True)
    assert world["told"] == ["sonnet"]

    voice_mode.mark(SID, False)
    voice_model.on_reply(SID, False)
    assert world["told"] == ["sonnet", "opus"] and world["model"] == "opus"
    assert voice_mode.switched(SID) is None
    assert _default(world) == "opus"


def test_a_busy_thread_is_not_switched_mid_turn(world):
    world["idle"] = False
    voice_mode.mark(SID, True)
    voice_model.on_reply(SID, True)
    assert world["told"] == [] and voice_mode.switched(SID) is None
    # The next voice reply, between turns, does it.
    world["idle"] = True
    voice_model.on_reply(SID, True)
    assert world["told"] == ["sonnet"]


def test_going_back_waits_for_the_turn_and_the_sweep_finishes_it(world):
    voice_mode.mark(SID, True)
    voice_model.on_reply(SID, True)
    world["idle"] = False
    voice_mode.mark(SID, False)
    voice_model.leave(SID)
    assert world["model"] == "sonnet" and voice_mode.switched(SID)
    world["idle"] = True
    voice_model.sweep()
    assert world["model"] == "opus" and voice_mode.switched(SID) is None


def test_a_stale_loop_is_swept_back(world):
    voice_mode.mark(SID, True)
    voice_model.on_reply(SID, True)
    voice_mode.mark(SID, True, now=0.0)  # long gone stale
    voice_model.sweep()
    assert world["model"] == "opus" and voice_mode.switched(SID) is None


def test_the_sweep_leaves_a_live_loop_alone(world):
    voice_mode.mark(SID, True)
    voice_model.on_reply(SID, True)
    voice_model.sweep()
    assert world["model"] == "sonnet" and voice_mode.switched(SID)


def test_a_model_changed_meanwhile_stands(world):
    voice_mode.mark(SID, True)
    voice_model.on_reply(SID, True)
    world["model"] = "haiku"  # the chip, or /model at the desk
    voice_mode.mark(SID, False)
    voice_model.on_reply(SID, False)
    assert world["told"] == ["sonnet"] and world["model"] == "haiku"
    assert voice_mode.switched(SID) is None


def test_already_on_the_quick_model_is_left_alone(world):
    world["model"] = "sonnet"
    voice_mode.mark(SID, True)
    voice_model.on_reply(SID, True)
    assert world["told"] == [] and voice_mode.switched(SID) is None


def test_switched_off_by_the_flag(world, monkeypatch):
    monkeypatch.setenv("MEDIA_VOICE_MODEL", "0")
    voice_mode.mark(SID, True)
    voice_model.on_reply(SID, True)
    assert world["told"] == [] and voice_mode.switched(SID) is None


def test_another_harness_keeps_its_model(world):
    world["agent"] = "codex"
    voice_mode.mark(SID, True)
    voice_model.on_reply(SID, True)
    assert world["told"] == [] and voice_mode.switched(SID) is None


def test_the_quick_model_is_configurable_per_harness(world, monkeypatch):
    monkeypatch.setenv("MEDIA_VOICE_MODEL_CLAUDE", "haiku")
    voice_mode.mark(SID, True)
    voice_model.on_reply(SID, True)
    assert world["told"] == ["haiku"]


def test_a_headless_thread_is_told_through_sessiond(world, monkeypatch):
    world["driver"] = driver.HEADLESS
    world["kept"] = ""  # no model chosen: its default
    told = []

    class Fake:
        def configure(self, session, *, model=None, mode=None):
            told.append(model)
            world["model"] = model or "opus"
            return True, {"told": True}

    monkeypatch.setattr(driver, "headless_driver", lambda: Fake())
    monkeypatch.setattr(sessions, "live_sessions", lambda: {})
    voice_mode.mark(SID, True)
    voice_model.on_reply(SID, True)
    voice_mode.mark(SID, False)
    voice_model.on_reply(SID, False)
    # Back to "" — set_model with no model, the session's own default.
    assert told == ["sonnet", ""]
    assert world["told"] == []  # nothing typed
    assert _default(world) == "opus"


def test_the_note_asks_for_opus_subagents_only_when_switched(world):
    voice_mode.mark(SID, True)
    payload = {"session_id": SID, "prompt": "what's next"}
    assert 'model: "opus"' not in heard.voice_note(payload)
    voice_model.on_reply(SID, True)
    note = heard.voice_note(payload)
    assert "short spoken sentences" in note and 'model: "opus"' in note
    voice_mode.mark(SID, False)
    assert heard.voice_note(payload) == ""


def test_restoring_the_default_keeps_an_absent_key_absent(world):
    world["settings"].write_text(json.dumps({"theme": "dark"}))
    voice_mode.mark(SID, True)
    voice_model.on_reply(SID, True)
    assert json.loads(world["settings"].read_text()) == {"theme": "dark"}
