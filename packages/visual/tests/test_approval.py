"""The question a session is stopped on, answered from the phone."""

import pytest

from agent_media_server import auth_abs, panes
from agent_media_visual import canvas, reply

# Claude Code's plan dialog, as a 34-column pane draws it (labels wrap).
CLAUDE = """  ──────────────────────────────
   Claude has written up a plan
   and is ready to execute.
   Would you like to proceed?
   ❯ 1. Yes, and use auto mode
     2. Yes, manually approve
        edits
     3. Tell Claude what to
        change
"""
# Codex, when hooks.json has changed.
CODEX = """  Hooks need review
  3 hooks are new or changed.
› 1. Review hooks
  2. Trust all and continue
  3. Continue without trusting
     (hooks won't run)
  Press enter to confirm or esc
"""
SID = "11111111-2222-3333-4444-555555555555"


def test_the_dialog_is_read_off_the_screen():
    d = reply.parse_dialog(CLAUDE)
    assert d["question"] == ("Claude has written up a plan and is ready to execute. "
                             "Would you like to proceed?")
    assert d["options"] == [
        {"n": 1, "label": "Yes, and use auto mode", "detail": ""},
        {"n": 2, "label": "Yes, manually approve", "detail": "edits"},
        {"n": 3, "label": "Tell Claude what to", "detail": "change"}]
    d = reply.parse_dialog(CODEX)
    assert d["question"] == "Hooks need review 3 hooks are new or changed."
    assert d["options"][2] == {"n": 3, "label": "Continue without trusting",
                               "detail": "(hooks won't run)"}


def test_a_screen_with_no_question_has_none():
    assert reply.parse_dialog("❯ \n  ⏵⏵ bypass permissions on") is None


def test_the_key_follows_the_words(monkeypatch):
    monkeypatch.setattr(reply, "_capture_pane", lambda p: CLAUDE)
    monkeypatch.setattr(panes, "classify", lambda cap, agent="claude": "approval")
    first = reply.approval_for("%7")
    assert first["key"] and first["agent"] == "claude"
    monkeypatch.setattr(reply, "_capture_pane", lambda p: CLAUDE.replace("auto mode", "AUTO MODE"))
    assert reply.approval_for("%7")["key"] != first["key"]


def test_nothing_to_answer_when_the_pane_is_not_waiting(monkeypatch):
    monkeypatch.setattr(reply, "_capture_pane", lambda p: CLAUDE)
    monkeypatch.setattr(panes, "classify", lambda cap, agent="claude": "working")
    assert reply.approval_for("%7") is None


@pytest.fixture
def _allowed(monkeypatch):
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.setattr(auth_abs, "may_reply", lambda u: (True, ""))


@pytest.fixture
def _live(monkeypatch, _allowed):
    monkeypatch.setattr(reply, "live_sessions", lambda: {SID: "%7"})
    monkeypatch.setattr(panes, "alive", lambda p: True)
    monkeypatch.setattr(reply, "_agent_of_pane", lambda p: "claude")
    monkeypatch.setattr(reply, "_capture_pane", lambda p: CLAUDE)
    monkeypatch.setattr(panes, "classify", lambda cap, agent="claude": "approval")
    keys = []
    monkeypatch.setattr(reply, "_tmux", lambda argv, timeout=10: keys.append(argv[-1]) or "")
    return keys


def test_answering_sends_a_number_and_enter(monkeypatch, _live):
    key = reply.approval_for("%7")["key"]
    answered = {"n": 0}

    def gone(pane, agent="claude"):
        # The dialog is gone the moment after the keys land.
        return None if answered["n"] else {"key": key, "options": []}

    monkeypatch.setattr(reply, "_tmux",
                        lambda argv, timeout=10: _live.append(argv[-1]) or answered.__setitem__("n", 1) or "")
    monkeypatch.setattr(reply, "approval_for", lambda pane, agent="claude": gone(pane, agent)
                        if answered["n"] else {"key": key, "agent": agent,
                                               "question": "q", "options": [{"n": 1, "label": "Yes"}]})
    ok, detail = reply.answer(SID, 1, key, "tok")
    assert ok and detail["answered"] == 1 and detail["label"] == "Yes"
    assert _live[-2:] == ["1", "Enter"]


def test_a_stale_answer_is_refused(_live):
    ok, detail = reply.answer(SID, 1, "not-the-key", "tok")
    assert ok is False and detail["status"] == 409 and "changed" in detail["error"]
    assert _live == []


def test_an_option_that_is_not_offered_is_refused(_live):
    key = reply.approval_for("%7")["key"]
    ok, detail = reply.answer(SID, 9, key, "tok")
    assert ok is False and detail["status"] == 400
    assert _live == []


def test_a_session_with_no_question_is_refused(monkeypatch, _live):
    monkeypatch.setattr(panes, "classify", lambda cap, agent="claude": "input")
    ok, detail = reply.answer(SID, 1, "", "tok")
    assert ok is False and detail["status"] == 409 and "not waiting" in detail["error"]
    assert _live == []


def test_a_dead_session_is_refused(monkeypatch, _allowed):
    monkeypatch.setattr(reply, "live_sessions", dict)
    ok, detail = reply.answer(SID, 1, "", "tok")
    assert ok is False and detail["status"] == 404


# A reply is often a numbered list; a dialog always marks the option the
# arrow keys are on.
NUMBERED_REPLY = """● Here is the plan:
  1. Read the file
  2. Change it
❯ 
  ⏵⏵ bypass permissions on
"""
REPLY_THEN_DIALOG = """● Steps:
  1. Read the file
  2. Change it
  ──────────────────────────────
   Do you want to proceed?
   ❯ 1. Yes
     2. No, keep planning
"""


CODEX_NUMBERED_REPLY = """• Here is the plan:
  1. Read the file
  2. Change it
› Ask Codex to do anything
  gpt-6-astra default · ~/scratch
"""


def test_a_numbered_reply_is_not_a_question():
    assert panes.classify(NUMBERED_REPLY, "claude") == "input"
    assert panes.classify(CODEX_NUMBERED_REPLY, "codex") == "input"


def test_the_marked_list_is_the_dialog():
    assert panes.classify(REPLY_THEN_DIALOG, "claude") == "approval"
    d = reply.parse_dialog(REPLY_THEN_DIALOG)
    assert d["question"] == "Do you want to proceed?"
    assert d["options"] == [{"n": 1, "label": "Yes", "detail": ""},
                            {"n": 2, "label": "No, keep planning", "detail": ""}]


def test_a_session_in_plan_mode_is_still_claude_code():
    # The dialog covers the footer, so the mode line is all there is.
    assert panes.classify("  ⏸ plan mode on (shift+tab to", "claude") == "input"


# The list can be longer than the pane, and then it scrolls: the top of the
# dialog — question included — is not on screen at all.
SCROLLED = """     and I'll plan the smallest
     edit to that actual file.
↓ 3. Stop planning
     The task is too small for
     plan mode
  ──────────────────────────────
  5. Chat about this

Enter to select · ↑/↓ to navigate
· Esc to cancel
"""


def test_a_scrolled_dialog_says_it_is_partial():
    assert panes.classify(SCROLLED, "claude") == "approval"
    d = reply.parse_dialog(SCROLLED)
    assert d["partial"] is True and d["question"] == ""
    assert [o["n"] for o in d["options"]] == [3, 5]


def test_a_whole_dialog_is_not_partial():
    assert reply.parse_dialog(CLAUDE)["partial"] is False


# Claude Code's own question to you, which is the same dialog: answers with a
# line of description each, and a list too long for the pane.
ASK = """Which colour do you prefer?
❯ 1. Red
     Warm, bold, high-energy.
  2. Green
     Natural, calm, balanced.
↓ 3. Blue
     Cool, steady, classic.
  ──────────────────────────────
  5. Chat about this
Enter to select · ↑/↓ to navigate
"""


def test_a_question_keeps_each_answer_s_description():
    d = reply.parse_dialog(ASK)
    assert d["question"] == "Which colour do you prefer?"
    assert d["options"][0] == {"n": 1, "label": "Red", "detail": "Warm, bold, high-energy."}
    # 4 is off the screen, so the list is not all of it — said, not guessed.
    assert [o["n"] for o in d["options"]] == [1, 2, 3, 5]
    assert d["partial"] is True
