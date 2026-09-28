"""The reply box's model and plan chips on a pane session (session_settings.py).

tmux is faked: a pane whose footer says what shift+tab has cycled it to, and
a list of what was typed. The headless side is in test_headless.py.
"""

from __future__ import annotations

import json

import pytest

from agent_media_server import auth, driver, panes, session_settings as ss, sessions, transcript

SID = "11111111-2222-3333-4444-555555555555"
CYCLE = ["", "⏵⏵ accept edits on (shift+tab to cycle)", "⏸ plan mode on (shift+tab to cycle)",
         "⏵⏵ bypass permissions on"]


@pytest.fixture()
def pane(monkeypatch, tmp_path):
    monkeypatch.delenv("MEDIA_HEADLESS", raising=False)
    driver._reset_for_tests()
    ss._CHOSEN.clear()
    ns = type("Pane", (), {})()
    ns.typed, ns.at, ns.state = [], 0, "waiting"
    ns.path = tmp_path / f"{SID}.jsonl"
    ns.path.write_text("")
    monkeypatch.setattr(auth, "gate", lambda b: ({"id": "u"}, {}))
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%7"})
    monkeypatch.setattr(sessions, "_agent_of_pane", lambda p: "claude")
    monkeypatch.setattr(sessions, "activity_of", lambda s, p, **k: {"state": ns.state})
    monkeypatch.setattr(panes, "alive", lambda p: True)
    monkeypatch.setattr(panes, "is_herdr", lambda p: False)
    monkeypatch.setattr(panes, "send", lambda p, t: ns.typed.append(t) or "")
    monkeypatch.setattr(panes, "capture", lambda p, lines=40, ansi=True: f"❯ \n─────\n  {CYCLE[ns.at]}\n")

    def keys(argv):
        assert argv[-1] == "BTab"
        ns.at = (ns.at + 1) % len(CYCLE)
        return ""
    monkeypatch.setattr(panes, "_tmux", keys)
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    monkeypatch.setattr(transcript, "transcript_path", lambda s: str(ns.path))
    return ns


def said(ns, model, stamp):
    with ns.path.open("a") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": stamp,
                            "message": {"model": model, "content": []}}) + "\n")


def test_reads_the_model_from_the_last_reply_and_plan_from_the_footer(pane):
    said(pane, "claude-opus-5-5", "2026-09-26T01:00:00Z")
    said(pane, "claude-sonnet-5", "2026-09-26T01:01:00Z")
    ok, d = ss.get(SID, "t")
    assert ok and d["model"] == "sonnet" and d["model_id"] == "claude-sonnet-5"
    assert d["plan"] is False and d["can"] == {"model": True, "plan": True}
    pane.at = 2
    assert ss.get(SID, "t")[1]["plan"] is True


def test_model_is_typed_as_the_desk_would_and_shows_until_a_reply(pane):
    said(pane, "claude-opus-5-5", "2020-01-01T00:00:00Z")
    ok, d = ss.post(SID, {"model": "haiku"}, "t")
    assert ok and pane.typed == ["/model haiku"] and d["model"] == "haiku"


def test_plan_on_cycles_shift_tab_until_the_footer_says_so_and_back_off(pane):
    ok, d = ss.post(SID, {"plan": True}, "t")
    assert ok and pane.at == 2 and d["plan"] is True
    ok, d = ss.post(SID, {"plan": False}, "t")
    assert ok and pane.at == 3 and d["plan"] is False
    ok, d = ss.post(SID, {"plan": False}, "t")      # already off: no keys
    assert ok and pane.at == 3


def test_a_working_pane_is_not_typed_into(pane):
    pane.state = "working"
    ok, d = ss.post(SID, {"model": "opus"}, "t")
    assert not ok and d["status"] == 409 and pane.typed == []


def test_other_agents_cannot(pane, monkeypatch):
    monkeypatch.setattr(sessions, "_agent_of_pane", lambda p: "codex")
    ok, d = ss.get(SID, "t")
    assert ok and d["can"] == {"model": False, "plan": False}


def test_a_new_chat_takes_only_a_sheet_alias_and_only_for_claude():
    assert ss.clean_new("claude", "Sonnet", "plan") == ("sonnet", "plan")
    assert ss.clean_new("claude", "gpt-9", "") == ("", "")
    assert ss.clean_new("codex", "opus", "plan") == ("", "")


def test_a_new_pane_chat_gets_the_flags(monkeypatch):
    from agent_media_server import send

    seen = {}
    monkeypatch.setattr(send, "_ask_pane", lambda text, **kw: seen.update(kw) or (True, {}))
    driver.pane_driver().start(agent="claude", cwd="/x", text="hi",
                               flags=["--dangerously-skip-permissions"], model="opus", mode="plan")
    # Bypass would win over plan; the allow- form keeps it in the cycle.
    assert seen["flags"] == ["--allow-dangerously-skip-permissions", "--model", "opus",
                             "--permission-mode", "plan"]


LISTING = """opencode/big-pickle
{"id": "big-pickle", "name": "Big Pickle", "cost": {"input": 0, "output": 0},
 "capabilities": {"toolcall": true}, "status": "active"}
opencode/claude-opus-5
{"id": "claude-opus-5", "name": "Claude Opus 5", "cost": {"input": 5, "output": 25},
 "capabilities": {"toolcall": true}}
openrouter/qwen/qwen3.8-27b:free
{"id": "qwen/qwen3.8-27b:free", "name": "Qwen3.8 27B (free)", "cost": {"input": 0, "output": 0},
 "capabilities": {"toolcall": true}}
openrouter/liquid/lfm-2.5-2.6b:free
{"id": "liquid/lfm-2.5-2.6b:free", "name": "LFM 2.5", "cost": {"input": 0, "output": 0},
 "capabilities": {"toolcall": false}}
gateway/claude-opus-4-8-liberated
{"id": "claude-opus-4-8-liberated", "name": "Opus via the gateway",
 "capabilities": {"toolcall": true}}
"""


def test_opencode_sheet_is_the_free_tool_users_its_own_first():
    from agent_media_core import opencode_models as om

    # A custom provider has no prices (reads 0) and is not "free"; a model
    # that cannot call tools is no agent; a paid Zen model is not listed.
    assert om.sheet(LISTING) == [
        {"id": "opencode/big-pickle", "label": "Big Pickle", "note": "Free"},
        {"id": "openrouter/qwen/qwen3.8-27b:free", "label": "Qwen3.8 27B (free)", "note": "Free"},
    ]
    assert om.default_note(om.sheet(LISTING), "opencode/big-pickle") == "Big Pickle · Free"
    assert om.default_note(om.sheet(LISTING), "gateway/x") == "gateway/x"


def test_an_opencode_chat_takes_a_model_id_and_nothing_else():
    assert ss.clean_new("opencode", "opencode/big-pickle", "plan") == ("opencode/big-pickle", "")
    assert ss.clean_new("opencode", "openrouter/qwen/qwen3.8-27b:free", "") == (
        "openrouter/qwen/qwen3.8-27b:free", "")
    for bad in ("--help", "big-pickle", "opencode/x; rm -rf ~", "opencode/$(id)", "a/-b"):
        assert ss.clean_new("opencode", bad, "") == ("", "")


def test_a_new_opencode_pane_chat_starts_on_the_model(monkeypatch):
    from agent_media_server import send

    seen = {}
    monkeypatch.setattr(send, "_ask_pane", lambda text, **kw: seen.update(kw) or (True, {}))
    driver.pane_driver().start(agent="opencode", cwd="/x", text="hi", model="opencode/big-pickle")
    assert seen["flags"] == ["-m", "opencode/big-pickle"]
    driver.pane_driver().start(agent="opencode", cwd="/x", text="hi")
    assert seen["flags"] == []


def test_a_new_chat_sheet_per_agent(monkeypatch):
    from agent_media_core import opencode_models as om

    monkeypatch.setattr(om, "models", lambda: (om.sheet(LISTING), "opencode/big-pickle"))
    oc = ss.new_sheet("opencode")
    assert [m["id"] for m in oc["models"]][0] == "opencode/big-pickle"
    assert oc["default"] == "Big Pickle · Free"
    assert ss.new_sheet("codex") == {"models": []}


OC = "ses_" + "a" * 26


@pytest.fixture()
def oc_pane(monkeypatch):
    """A running opencode whose picker does what the desk's does: `/models`,
    the name typed, Enter — and the composer's footer says the pick."""
    from agent_media_core import opencode_models as om

    driver._reset_for_tests()
    ss._CHOSEN.clear()
    ns = type("Pane", (), {})()
    ns.typed, ns.keys, ns.on, ns.state, ns.picker = [], [], "Big Pickle", "waiting", False
    ns.takes = True
    monkeypatch.setattr(auth, "gate", lambda b: ({"id": "u"}, {}))
    monkeypatch.setattr(sessions, "live_sessions", lambda: {OC: "%9"})
    monkeypatch.setattr(sessions, "_agent_of_pane", lambda p: "opencode")
    monkeypatch.setattr(sessions, "agent_of", lambda s: "opencode")
    monkeypatch.setattr(sessions, "activity_of", lambda s, p, **k: {"state": ns.state})
    monkeypatch.setattr(panes, "alive", lambda p: True)
    monkeypatch.setattr(panes, "is_herdr", lambda p: False)

    def send(p, t):
        ns.typed.append(t)
        ns.picker = t == "/models"
        return ""

    def typed(p, t):
        ns.typed.append(t)
        ns.search = t

    def tmux(argv):
        ns.keys.append(argv[-1])
        if argv[-1] == "Enter" and ns.picker and ns.takes:
            ns.on = ns.search
        ns.picker = False
        return ""
    monkeypatch.setattr(panes, "send", send)
    monkeypatch.setattr(panes, "_type_tmux", typed)
    monkeypatch.setattr(panes, "_tmux", tmux)
    monkeypatch.setattr(panes, "capture", lambda p, lines=40, ansi=True: (
        "     ▣  Build · Big Pickle · 5.2s\n  ┃\n"
        f"  ┃  Build · {ns.on} OpenCode Zen                          ~/work\n  ╹▀▀▀\n"))
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    monkeypatch.setattr(om, "models", lambda: (om.sheet(LISTING), "opencode/big-pickle"))
    monkeypatch.setattr(om, "current", lambda s: "opencode/big-pickle")
    monkeypatch.setattr(ss, "_opencode_at", lambda s: 0.0)
    return ns


def test_a_running_opencode_says_its_model_and_offers_the_free_ones(oc_pane):
    ok, d = ss.get(OC, "t")
    assert ok and d["agent"] == "opencode" and d["model"] == "opencode/big-pickle"
    assert d["can"] == {"model": True, "plan": False}
    assert [m["id"] for m in d["models"]] == ["opencode/big-pickle", "openrouter/qwen/qwen3.8-27b:free"]


def test_a_model_not_free_here_heads_the_sheet(oc_pane, monkeypatch):
    from agent_media_core import opencode_models as om

    monkeypatch.setattr(om, "current", lambda s: "gateway/claude-opus-4-8")
    ok, d = ss.get(OC, "t")
    assert d["models"][0] == {"id": "gateway/claude-opus-4-8", "label": "claude-opus-4-8", "note": ""}


def test_opencode_is_switched_through_its_picker(oc_pane):
    ok, d = ss.post(OC, {"model": "openrouter/qwen/qwen3.8-27b:free"}, "t")
    assert ok, d
    assert oc_pane.typed == ["/models", "Qwen3.8 27B (free)"] and oc_pane.keys == ["Enter"]
    assert d["model"] == "openrouter/qwen/qwen3.8-27b:free" and d["told"]


def test_opencode_that_does_not_take_it_is_left_as_it_was(oc_pane):
    oc_pane.takes = False
    ok, d = ss.post(OC, {"model": "openrouter/qwen/qwen3.8-27b:free"}, "t")
    assert not ok and d["status"] == 502 and "did not take" in d["error"]
    assert oc_pane.keys == ["Enter", "Escape"]
    assert ss.get(OC, "t")[1]["model"] == "opencode/big-pickle"


def test_opencode_refuses_mid_turn_plan_and_strangers(oc_pane):
    oc_pane.state = "working"
    assert ss.post(OC, {"model": "opencode/big-pickle"}, "t")[1]["status"] == 409
    oc_pane.state = "waiting"
    assert ss.post(OC, {"plan": True}, "t")[1]["status"] == 400
    assert ss.post(OC, {"model": "--help"}, "t")[1]["status"] == 400
    ok, d = ss.post(OC, {"model": "opencode/not-on-the-sheet"}, "t")
    assert not ok and d["status"] == 400 and not oc_pane.typed
