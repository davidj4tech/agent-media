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
