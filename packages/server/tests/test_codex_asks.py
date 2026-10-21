"""Codex 0.163 question overlays, from OpenAI's public TUI snapshots.

Fixtures: codex-rs/tui/src/bottom_pane/request_user_input/snapshots/,
request_user_input_options and request_user_input_options_notes_visible.
Key semantics are taken from request_user_input/mod.rs: digits commit and
advance; Down and Tab select an option and open its notes without committing.
"""
import json
from pathlib import Path

import pytest

from agent_media_server import asks, codex_asks, panes, send, sessions

FIX = Path(__file__).parent / "fixtures" / "codex_asks"
SID = "11111111-2222-3333-4444-555555555555"


def cap(name="options"):
    return (FIX / f"{name}.txt").read_text()


def test_real_options_and_notes_snapshots():
    s = codex_asks.parse(cap())
    assert s["question"] == "Choose an option."
    assert s["index"] == 0 and s["total"] == 1 and not s["partial"]
    assert s["options"][0] == {"n": 1, "label": "Option 1", "detail": "First choice."}
    assert s["options"][-1]["label"] == "None of the above"
    assert s["cursor"] == 1 and s["notes"] is None
    assert codex_asks.parse(cap("notes"))["notes"] == "Add notes"
    assert panes.classify(cap(), "codex") == "approval"
    a = codex_asks.approval(cap())
    assert a["kind"] == "question" and a["free_text"]
    assert len(a["questions"][0]["options"]) == 3
    assert not codex_asks.approval(cap("notes"))["free_text"]


def test_permissions_are_not_question_cards():
    assert codex_asks.approval("Would you like to run this command?\n› 1. Yes, proceed\n  2. No") is None


def write_request(tmp_path, monkeypatch, completed=False):
    from agent_media_core import harnesses
    qs = [{"id": "area", "header": "Area", "question": "Choose an option.", "isOther": True,
           "options": [{"label": f"Option {i}", "description": "detail"} for i in range(1, 4)]},
          {"id": "next", "header": "Next", "question": "What next?", "isOther": True,
           "options": [{"label": "Tests", "description": "Run them"}]}]
    path = tmp_path / "rollout.jsonl"
    records = [{"type": "response_item", "payload": {"type": "function_call",
                "name": "request_user_input", "call_id": "ask1", "arguments": json.dumps({"questions": qs})}}]
    if completed:
        records.append({"type": "response_item", "payload": {"type": "function_call_output", "call_id": "ask1"}})
    path.write_text("\n".join(json.dumps(r) for r in records))
    monkeypatch.setattr(harnesses, "transcript", lambda session: ("codex", path))
    return qs


def test_rollout_keeps_all_questions_and_key_across_tabs(tmp_path, monkeypatch):
    write_request(tmp_path, monkeypatch)
    first = codex_asks.approval(cap().replace("1/1", "1/2"), SID)
    second = codex_asks.approval("Question 2/2 (1 unanswered)\nWhat next?\n› 1. Tests  Run them\n2. None of the above\nenter to submit all", SID)
    assert first["source"] == second["source"] == "rollout"
    assert first["key"] == second["key"] and second["current"] == 1
    assert len(first["questions"]) == 2
    changed = codex_asks.approval(cap().replace("1/1", "1/2").replace("Choose an option.", "Different?"), SID)
    assert changed["source"] == "screen" and changed["key"] != first["key"]


def test_completed_request_is_not_reused(tmp_path, monkeypatch):
    write_request(tmp_path, monkeypatch, completed=True)
    assert codex_asks.approval(cap().replace("1/1", "1/2"), SID)["source"] == "screen"


class FakeCodex:
    """A digit immediately commits; any extra Enter submits the next tab."""
    def __init__(self, questions):
        self.questions = questions
        self.index = 0
        self.cursor = 1
        self.notes = None
        self.sent = []
        self.keys = []

    def read(self):
        if self.index == len(self.questions):
            return None
        return {"index": self.index, "total": len(self.questions),
                "question": self.questions[self.index]["question"],
                "options": self.questions[self.index]["options"] + [{"n": 4, "label": "None of the above"}],
                "cursor": self.cursor, "notes": self.notes}

    def commit(self, value):
        self.sent.append(value)
        self.index += 1
        self.cursor = 1
        self.notes = None

    def key(self, key):
        self.keys.append(key)
        if key.isdigit():
            self.commit(int(key))
        elif key == "Down":
            self.cursor += 1
        elif key == "Tab":
            self.notes = "Add notes"
        elif key == "Enter":
            self.commit(self.notes or self.cursor)
        elif key == "Left":
            self.index -= 1

    def type(self, text):
        assert self.notes == "Add notes"
        self.notes = text

    def sleep(self, seconds):
        pass


def test_choices_do_not_send_an_extra_enter_to_next_question():
    q = codex_asks.approval(cap())["questions"][0]
    screen = FakeCodex([q, {**q, "question": "Next?"}])
    codex_asks.drive(screen, screen.questions, [{"selected": [2], "other": ""}, {"selected": [3], "other": ""}])
    assert screen.sent == [2, 3] and screen.keys == ["2", "3"]


def test_free_text_opens_notes_before_typing():
    q = codex_asks.approval(cap())["questions"][0]
    screen = FakeCodex([q])
    codex_asks.drive(screen, [q], [{"selected": [], "other": "My own answer"}])
    assert screen.sent == ["My own answer"]
    assert screen.keys == ["Down", "Down", "Down", "Tab", "Enter"]


def test_changed_question_never_gets_keys():
    q = codex_asks.approval(cap())["questions"][0]
    screen = FakeCodex([{**q, "question": "Changed?"}])
    with pytest.raises(asks.Stuck, match="changed"):
        codex_asks.drive(screen, [q], [{"selected": [1], "other": ""}])
    assert not screen.keys


def test_stale_card_is_refused_before_codex_driver(monkeypatch):
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%fake"})
    monkeypatch.setattr(panes, "alive", lambda p: True)
    monkeypatch.setattr(sessions, "_agent_of_pane", lambda p: "codex")
    monkeypatch.setattr(sessions, "approval_for", lambda *a: codex_asks.approval(cap()))
    ok, detail = send._answer_pane(SID, 1, "old-key")
    assert not ok and detail["status"] == 409


def test_hook_keeps_all_parts_without_a_flushed_rollout(monkeypatch):
    from agent_media_core import harnesses, pending_asks
    monkeypatch.setattr(harnesses, "transcript", lambda session: None)
    pending_asks.record(SID, "codex-hook-1", {"questions": [
        {"question": "Choose an option.", "options": [{"label": f"Option {i}"} for i in range(1, 4)]},
        {"question": "Next?", "options": [{"label": "Tests"}]}]}, harness="codex")
    a = codex_asks.approval(cap().replace("1/1", "1/2"), SID)
    assert a["source"] == "hook" and a["tool_use_id"] == "codex-hook-1"
    assert len(a["questions"]) == 2


def test_choice_never_types_into_open_notes():
    q = codex_asks.approval(cap())["questions"][0]
    screen = FakeCodex([q])
    screen.notes = "Add notes"
    with pytest.raises(asks.Stuck, match="notes composer"):
        codex_asks.drive(screen, [q], [{"selected": [1], "other": ""}])
    assert not screen.keys


def test_numbered_answer_routes_through_codex_without_enter(monkeypatch):
    dialog = codex_asks.approval(cap())
    screen = FakeCodex(dialog["questions"])
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%fake"})
    monkeypatch.setattr(panes, "alive", lambda p: True)
    monkeypatch.setattr(sessions, "_agent_of_pane", lambda p: "codex")
    monkeypatch.setattr(sessions, "approval_for", lambda *a: dialog if screen.read() else None)
    monkeypatch.setattr(codex_asks, "Screen", lambda *a: screen)
    ok, result = send._answer_pane(SID, 2, dialog["key"])
    assert ok and not result["waiting"]
    assert screen.sent == [2] and screen.keys == ["2"]


def test_wrapped_free_text_and_scrolled_footer():
    text = cap("notes").replace("› Add notes", "› A long answer\n    continued on another line")
    assert codex_asks.parse(text)["notes"] == "A long answer continued on another line"
    text = cap().replace("tab to add notes", "option 1/8 | tab to add notes")
    assert codex_asks.parse(text)["partial"]
