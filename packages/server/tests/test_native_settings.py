"""Native model catalogues and settings: no live CLIs, players or sessions."""

import json

import pytest

from agent_media_core import harnesses, native_models
from agent_media_server import auth, codex_control, driver, native_settings as ns, panes, session_settings as ss, sessions

SID = "11111111-2222-3333-4444-555555555555"


def test_catalogue_filters_hidden_models_and_never_returns_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-example", "display_name": "Example", "description": "Fast", "api_key": "SECRET"},
        {"slug": "hidden", "visibility": "hide"}, {"slug": "--flag"}]}))
    assert native_models.models("codex") == [{"id": "gpt-example", "label": "Example", "note": "Fast"}]
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": {
        "x": {"info": {"id": "grok-example", "name": "Example"}, "api_key": "SECRET"}}}))
    assert native_models.models("grok") == [{"id": "grok-example", "label": "Example", "note": ""}]
    (tmp_path / "models_cache.json").write_text("not JSON")
    assert native_models.models("grok") == []


def test_codex_context_is_read_instead_of_assistant_message_shape(monkeypatch, tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(json.dumps({"type": "turn_context", "timestamp": "2026-10-20T00:00:00Z",
                                "payload": {"model": "gpt-example"}}) + "\n")
    monkeypatch.setattr(harnesses, "transcript", lambda s: ("codex", path))
    model, at = native_models.current(SID)
    assert model == "gpt-example" and at > 0


@pytest.fixture
def codex(monkeypatch):
    monkeypatch.setattr(codex_control, "state", lambda s: None)
    class UI:
        phase = "idle"
        model = "old-model"
        plan = False
        typed = []
        keys = []
        state = "waiting"

    ui = UI()
    monkeypatch.setattr(ns.time, "sleep", lambda s: None)
    monkeypatch.setattr(panes, "is_herdr", lambda p: False)
    def capture(*a, **k):
        if ui.phase == "models":
            return "Select Model and Effort\n› 1. Example (current)\nenter select · esc back"
        if ui.phase == "reasoning":
            return "Select Reasoning Level for Example\n› 1. Low\nenter default · s session · esc back"
        return "› \n" + ui.model + (" · Plan mode (shift+tab to cycle)" if ui.plan else " · Default mode")
    def send(p, text):
        ui.typed.append(text)
        ui.phase = "models"
        return ""
    def key(argv):
        key = argv[-1]
        ui.keys.append(key)
        if key == "Enter" and ui.phase == "models":
            ui.phase = "reasoning"
        elif key == "s" and ui.phase == "reasoning":
            ui.model, ui.phase = "gpt-example", "idle"
        elif key == "Escape":
            ui.phase = "idle"
        elif key == "BTab":
            ui.plan = not ui.plan
    monkeypatch.setattr(panes, "capture", capture)
    monkeypatch.setattr(panes, "send", send)
    monkeypatch.setattr(panes, "_tmux", key)
    return ui


OFFERED = [{"id": "gpt-example", "label": "Example", "note": ""}]


def test_codex_uses_picker_and_session_action(codex):
    ok, d = ns.configure("codex", "%7", model="gpt-example", mode="plan", offered=OFFERED)
    assert ok, d
    assert codex.typed == ["/model"]
    assert codex.keys == ["Enter", "s", "BTab"]
    assert codex.model == "gpt-example" and codex.plan
    assert ns.configure("codex", "%7", model=None, mode="", offered=OFFERED)[0]
    assert not codex.plan


def test_unknown_model_is_rejected_before_any_input(codex):
    assert not ns.configure("codex", "%7", model="stranger", mode=None, offered=OFFERED)[0]
    assert not codex.typed


def test_picker_without_matching_row_is_closed_without_selection(codex):
    offered = [{"id": "missing", "label": "Missing"}]
    assert not ns.configure("codex", "%7", model="missing", mode=None, offered=offered)[0]
    assert "Enter" not in codex.keys and codex.keys[-1] == "Escape"


def test_grok_plan_rejected_before_model_change(codex):
    assert not ns.configure("grok", "%7", model="gpt-example", mode="plan", offered=OFFERED)[0]
    assert not codex.typed


def test_codex_route_capabilities_changes_and_busy_refusal(codex, monkeypatch):
    monkeypatch.delenv("MEDIA_HEADLESS", raising=False)
    driver._reset_for_tests()
    ss._CHOSEN.clear()
    monkeypatch.setattr(auth, "gate", lambda b: ({"id": "u"}, {}))
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%7"})
    monkeypatch.setattr(sessions, "_agent_of_pane", lambda p: "codex")
    monkeypatch.setattr(sessions, "agent_of", lambda s: "codex")
    monkeypatch.setattr(sessions, "activity_of", lambda *a: {"state": codex.state})
    monkeypatch.setattr(panes, "alive", lambda p: True)
    monkeypatch.setattr(native_models, "current", lambda s: ("old-model", 0.0))
    monkeypatch.setattr(harnesses, "profile_of", lambda s: "")
    monkeypatch.setattr(native_models, "models", lambda *a: OFFERED)
    assert ss.get(SID, "token")[1]["can"] == {"model": True, "plan": True}
    assert ss.new_sheet("codex")["can"] == {"model": True, "plan": True}
    assert ss.new_sheet("grok")["can"] == {"model": True, "plan": False}
    codex.state = "working"
    assert ss.post(SID, {"model": "gpt-example"}, "token")[1]["status"] == 409
    assert not codex.typed
    codex.state = "waiting"
    ok, result = ss.post(SID, {"model": "gpt-example", "plan": True}, "token")
    assert ok, result
    assert result["model"] == "gpt-example" and result["plan"]
    assert ss.post(SID, {"model": "haiku"}, "token")[1]["status"] == 400
    ss._CHOSEN.clear()


def test_new_codex_and_grok_flags(monkeypatch):
    from agent_media_server import driver, send

    seen = {}
    monkeypatch.setattr(send, "_ask_pane", lambda text, **kw: seen.update(kw) or (True, {}))
    driver.pane_driver().start(agent="codex", cwd="/x", text="hi", model="gpt-example", mode="plan")
    assert seen["flags"] == ["--model", "gpt-example"] and seen["plan"] is True
    seen.clear()
    driver.pane_driver().start(agent="grok", cwd="/x", text="hi", model="grok-example")
    assert seen["flags"] == ["--model", "grok-example"] and "plan" not in seen


def test_shared_daemon_route_needs_no_terminal_mapping(monkeypatch):
    monkeypatch.delenv("MEDIA_HEADLESS", raising=False)
    driver._reset_for_tests()
    monkeypatch.setattr(sessions, "live_sessions", lambda: {})
    monkeypatch.setattr(sessions, "agent_of", lambda s: "codex")
    monkeypatch.setattr(codex_control, "state", lambda s: {
        "model": "gpt-example", "plan": True, "mode_known": True, "status": "idle"})
    assert ss.state(SID) == {"agent": "codex", "live": True, "model": "gpt-example",
                             "model_id": "gpt-example", "plan": True, "driver": "codex",
                             "can": {"model": True, "plan": True}}


@pytest.fixture
def daemon(monkeypatch):
    from contextlib import contextmanager

    class Fake:
        status = "idle"
        model = "old-model"
        plan = False
        calls = []

        def read(self, session):
            return {"model": self.model, "plan": self.plan, "effort": "high",
                    "mode_known": True, "status": self.status}

        def call(self, method, params):
            self.calls.append((method, params))
            if "model" in params:
                self.model = params["model"]
            if "collaborationMode" in params:
                self.plan = params["collaborationMode"]["mode"] == "plan"
            return {}

    fake = Fake()
    @contextmanager
    def connect(s):
        yield fake
    monkeypatch.setattr(codex_control, "connect", connect)
    return fake


def test_daemon_changes_only_model_and_collaboration_settings(daemon):
    assert codex_control.configure(SID, model="gpt-example", mode="plan")[0]
    assert daemon.calls == [("thread/settings/update", {
        "threadId": SID, "model": "gpt-example", "collaborationMode": {
            "mode": "plan", "settings": {"model": "gpt-example", "reasoning_effort": "high",
                                            "developer_instructions": None}}})]
    assert codex_control.configure(SID, model=None, mode="")[0]
    assert daemon.calls[-1][1]["collaborationMode"]["mode"] == "default"
    assert "model" not in daemon.calls[-1][1]


def test_daemon_refuses_working_or_question_threads_without_writes(daemon):
    daemon.status = "active"
    assert codex_control.configure(SID, model="gpt-example", mode=None)[1]["status"] == 409
    assert not daemon.calls
