"""A question and its answer are turns; the harness's asides are not.

2026-09-09: the Sasonica chat showed an answer acting on an instruction with
no question above it, and read 230KB of task-notification XML aloud in the
listener's voice. Both are shape problems in what reaches the transcript.
"""
import pytest

from agent_media_core.intake import hook_claude_code as H


# --- the harness's asides ----------------------------------------------------

@pytest.mark.parametrize("raw, want", [
    ("<task-notification><task-id>b1</task-id></task-notification>", ""),
    ("[SYSTEM NOTIFICATION - NOT USER INPUT]\nA background task finished.", ""),
    ("<local-command-stdout>Set model to Opus</local-command-stdout>\nwhy is it repeating?",
     "why is it repeating?"),
    ("<system-reminder>be brief</system-reminder> please continue", "please continue"),
    ("ordinary words", "ordinary words"),
])
def test_only_what_the_listener_typed_is_kept(raw, want):
    assert H.strip_system_blocks(raw) == want


def test_a_truncated_block_takes_its_tail_with_it():
    """The harness cuts long blocks off mid-way; what follows an opening tag
    is still not something anybody said."""
    assert H.strip_system_blocks("<task-notification><task-id>b1</task-id> …") == ""
    assert H.strip_system_blocks("real words <task-notification>b1") == "real words"


def test_a_prompt_that_is_only_a_notification_records_nothing(monkeypatch):
    recorded = []
    monkeypatch.setenv("MEDIA_HOOK_NO_DETACH", "1")
    monkeypatch.setattr(H, "_record_listener_text",
                        lambda s, t: recorded.append((s, t)) or 0)
    H._handle_user_prompt({"prompt": "<task-notification><task-id>x</task-id></task-notification>",
                           "session_id": "s1"})
    assert not recorded
    H._handle_user_prompt({"prompt": "and now the real question?", "session_id": "s1"})
    assert recorded == [("s1", "and now the real question?")]


# --- the question ------------------------------------------------------------

TOOL_INPUT = {"questions": [{
    "question": "Which case do you mean?",
    "header": "Mini player",
    "multiSelect": False,
    "options": [{"label": "In the app", "description": "red5 started it here"},
                {"label": "On red5", "description": "the rooms"},
                {"label": "", "description": "dropped: no label"}],
}]}


def test_the_question_is_kept_as_structure_not_just_a_sentence():
    got = H.ask_structure(TOOL_INPUT)
    assert got == [{"question": "Which case do you mean?",
                    "multiSelect": False,
                    "options": [{"label": "In the app", "description": "red5 started it here"},
                                {"label": "On red5", "description": "the rooms"}]}]
    assert H.ask_structure({"questions": [{"question": "  "}]}) == []


def test_each_option_is_spoken_with_its_description():
    got = H._format_ask_question(TOOL_INPUT)
    assert got == ("Which case do you mean? Option 1: In the app. Red5 started it here."
                   " Option 2: On red5. The rooms.")
    bare = {"questions": [{"question": "Q?", "options": [{"label": "A"},
                                                         {"label": "B", "description": "ends here."}]}]}
    assert H._format_ask_question(bare) == "Q? Option 1: A. Option 2: B. Ends here."
    recommended = {"questions": [{"question": "Q?", "options": [
        {"label": "Keep it (Recommended)", "description": "no change"}]}]}
    assert H._format_ask_question(recommended) == "Q? Option 1: Keep it (Recommended). No change."


def test_spoken_option_numbers_are_the_cards():
    # The card numbers options by place (asks.py `n`) and hands-free picks by
    # the number heard: an option with no label is not read, but the ones
    # after it keep their place's number.
    gap = {"questions": [{"question": "Q?", "options": [{"label": "A"}, {"label": "  "},
                                                        {"label": "C"}]}]}
    assert H._format_ask_question(gap) == "Q? Option 1: A. Option 3: C."


def test_the_spoken_question_carries_the_structure(monkeypatch):
    seen = {}
    monkeypatch.setattr(H, "_emit_ask",
                        lambda ask, payload, lead="", structure=None:
                        seen.update(ask=ask, structure=structure) or 0)
    H._handle_pretooluse({"tool_name": "AskUserQuestion", "tool_input": TOOL_INPUT})
    assert "Which case do you mean?" in seen["ask"]      # still speakable
    assert seen["structure"][0]["options"][0]["label"] == "In the app"


# --- the answer --------------------------------------------------------------

@pytest.mark.parametrize("response, want", [
    ('Your questions have been answered: "Which case?"="In the app"', "In the app"),
    ({"answers": {"Which case?": "In the app"}}, "In the app"),
    ({"answers": [{"question": "Which?", "answer": "In the app"}]}, "In the app"),
    ({"answers": {"a": "One", "b": "Two"}}, "One, Two"),
    ({"content": 'x "Q"="In the app"'}, "In the app"),
    ({}, ""),
    (None, ""),
])
def test_the_chosen_option_is_read_back_whatever_shape_it_arrives_in(response, want):
    assert H.answers_from_response(response) == want


def test_the_answer_is_recorded_as_a_listener_turn(monkeypatch):
    recorded = []
    monkeypatch.setattr(H, "_record_listener_text",
                        lambda s, t: recorded.append((s, t)) or 0)
    H._handle_posttooluse({"tool_name": "AskUserQuestion", "session_id": "s1",
                           "tool_response": {"answers": {"Which case?": "In the app"}}})
    assert recorded == [("s1", "In the app")]
    recorded.clear()
    H._handle_posttooluse({"tool_name": "Bash", "session_id": "s1", "tool_response": {}})
    assert not recorded



def test_a_question_answered_by_typing_is_cleared_and_records_nothing(monkeypatch):
    """Typing instead of choosing fails the tool call: PostToolUseFailure,
    not PostToolUse. The read-out must still be cut and dropped (it played
    six minutes later when the thread was opened, 18 Oct 2026), and the
    error text is not the listener's words."""
    recorded, cut, dropped, cleared = [], [], [], []
    monkeypatch.setattr(H, "_record_listener_text",
                        lambda s, t: recorded.append((s, t)) or 0)
    monkeypatch.setattr(H, "_stop_playing_ask", lambda s: None)
    monkeypatch.setattr(H, "load_env_file", lambda *a: None)
    from agent_media_core import pending_asks
    from agent_media_core.intake import submit, toast
    monkeypatch.setattr(pending_asks, "clear", lambda s, t="": cleared.append((s, t)))
    monkeypatch.setattr(submit, "request_session_speech_cut",
                        lambda s, why: cut.append((s, why)))
    monkeypatch.setattr(toast, "drop_asks", lambda s: dropped.append(s))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(__import__("json").dumps({
        "hook_event_name": "PostToolUseFailure", "tool_name": "AskUserQuestion",
        "session_id": "s1", "tool_use_id": "t1",
        "error": "The user answered by typing a message instead of choosing"})))
    assert H.main() == 0
    assert cleared == [("s1", "t1")]
    assert cut == [("s1", "ask")]
    assert dropped == ["s1"]
    assert not recorded

# --- slash commands ----------------------------------------------------------

def test_a_work_command_becomes_a_turn_and_a_settings_one_does_not(monkeypatch):
    recorded = []
    monkeypatch.setattr(H, "_record_listener_text",
                        lambda s, t, extras=None: recorded.append((t, extras)) or 0)
    monkeypatch.setattr(H, "_handle_user_prompt", H._handle_user_prompt)

    H._handle_user_prompt({"prompt": "/code-review high", "session_id": "s1"})
    assert recorded[-1][0] == "/code-review high"
    assert recorded[-1][1]["command"]["name"] == "code-review"

    recorded.clear()
    H._handle_user_prompt({"prompt": "<command-name>/model</command-name>"
                                     "<command-args></command-args>", "session_id": "s1"})
    assert not recorded


def test_a_path_is_not_a_command(monkeypatch):
    recorded = []
    monkeypatch.setattr(H, "_record_listener_text",
                        lambda s, t, extras=None: recorded.append((t, extras)) or 0)
    H._handle_user_prompt({"prompt": "/home/ryer/notes.md is the one",
                           "session_id": "s1"})
    assert recorded == [("/home/ryer/notes.md is the one", None)]
