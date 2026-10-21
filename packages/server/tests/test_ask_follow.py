"""The later parts of a question card, spoken as each comes up (ask_follow.py).

David, 19 Oct 2026: "in multi-part questions, can the next part not be read
until the previous one is answered". The app says when a part is picked; the
watcher reads the terminal's tabs. Nothing here speaks: speak_ask_part is
replaced by a recorder.
"""
import os

import pytest

from agent_media_core import pending_asks
from agent_media_core.intake import hook_claude_code as hook
from agent_media_server import ask_follow, auth, sessions

S1 = "0a1b2c3d-0000-4000-8000-000000000001"
S2 = "0a1b2c3d-0000-4000-8000-000000000002"

CARD = {"questions": [
    {"question": "Which colour?", "options": [{"label": "Red"}, {"label": "Blue"}]},
    {"question": "Which pet?", "options": [{"label": "Cat"}, {"label": "Dog"}]},
    {"question": "Which size?", "options": [{"label": "S"}, {"label": "L"}]},
]}


@pytest.fixture
def spoken(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith(("MEDIA_", "RELAY_")):
            monkeypatch.delenv(k, raising=False)
    for d in ("HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(d, str(tmp_path / d.lower()))
    said = []

    def speak(session, part, tool_use_id=""):
        if not pending_asks.claim_part(session, "t1", part):
            return "already_spoken"
        said.append((session, part))
        return ""

    monkeypatch.setattr(hook, "speak_ask_part", speak)
    monkeypatch.setattr(auth, "gate", lambda bearer: ("david", None) if bearer == "ok"
                        else (None, {"error": "unauthorised", "status": 401}))
    pending_asks.record(S1, "t1", CARD)
    return said


def test_a_part_picked_in_the_app_speaks_the_next(spoken):
    ok, detail = ask_follow.post({"session": S1, "part": 1, "tool_use_id": "t1"}, "ok")
    assert ok and detail == {"spoken": True}
    assert spoken == [(S1, 1)]
    ok, detail = ask_follow.post({"session": S1, "part": 1}, "ok")
    assert ok and detail == {"spoken": False, "why": "already_spoken"}


def test_the_question_alone_finds_its_thread(spoken):
    ok, detail = ask_follow.post({"part": 2, "tool_use_id": "t1"}, "ok")
    assert ok and detail == {"spoken": True}
    assert spoken == [(S1, 2)]
    assert ask_follow.post({"part": 2, "tool_use_id": "t-gone"}, "ok")[1]["why"] == "no_question"
    assert not ask_follow.post({"part": 2, "tool_use_id": "t-gone"}, "bad")[0]


@pytest.mark.parametrize("body, bearer, status", [
    ({"session": S1, "part": 1}, "bad", 401),
    ({"session": "../x", "part": 1}, "ok", 400),
    ({"session": S1, "part": "two"}, "ok", 400),
])
def test_refused(spoken, body, bearer, status):
    ok, detail = ask_follow.post(body, bearer)
    assert not ok and detail["status"] == status
    assert not spoken


def test_nothing_is_spoken_for_another_question_or_part(spoken):
    assert ask_follow.post({"session": S1, "part": 1, "tool_use_id": "t9"}, "ok")[1]["why"] \
        == "not_this_question"
    assert ask_follow.post({"session": S1, "part": 0}, "ok")[1]["why"] == "no_such_part"
    assert ask_follow.post({"session": S2, "part": 1}, "ok")[1]["why"] == "no_question"
    assert not spoken


def test_the_watcher_speaks_each_tab_the_terminal_reaches(spoken, monkeypatch):
    screens = [0, 0, 1, 1, 2, "review", None]

    def approval_for(pane, agent, session):
        at = screens.pop(0) if screens else None
        if at is None:
            pending_asks.clear(S1, "t1")          # answered: PostToolUse
            return None
        return {"kind": "question", "tool_use_id": "t1", "review": at == "review",
                "current": 0 if at == "review" else at}

    monkeypatch.setattr(sessions, "approval_for", approval_for)
    monkeypatch.setattr(ask_follow, "POLL_S", 0)
    ask_follow.follow(S1, "%9", "t1")
    assert spoken == [(S1, 1), (S1, 2)]


def test_the_watcher_gives_up_when_the_card_is_gone(spoken, monkeypatch):
    monkeypatch.setattr(sessions, "approval_for", lambda *a: None)
    monkeypatch.setattr(ask_follow, "POLL_S", 0)
    monkeypatch.setattr(ask_follow, "GONE_S", 0)
    ask_follow.follow(S1, "%9", "t1")
    assert not spoken


def test_codex_watcher_reads_codex_overlay(spoken, monkeypatch):
    pending_asks.update(S1, "t1", harness="codex")
    seen = []
    def approval(pane, agent, session):
        seen.append(agent)
        if len(seen) > 1:
            pending_asks.clear(S1, "t1")
            return None
        return {"kind": "question", "tool_use_id": "t1", "current": 1}
    monkeypatch.setattr(sessions, "approval_for", approval)
    monkeypatch.setattr(ask_follow, "POLL_S", 0)
    ask_follow.follow(S1, "%9", "t1")
    assert seen == ["codex", "codex"] and spoken == [(S1, 1)]
