"""A card of several questions is read one part at a time.

David, 19 Oct 2026: "in multi-part questions, can the next part not be read
until the previous one is answered". The PreToolUse hook speaks the first
part; the rest are spoken by `speak_ask_part`, once each, when the app or
the terminal says the part before was answered.
"""
import pytest

from agent_media_core import pending_asks
from agent_media_core.intake import hook_claude_code as H

CARD = {"questions": [
    {"question": "Which colour?", "header": "Colour",
     "options": [{"label": "Red (Recommended)", "description": "warm"}, {"label": "Blue"}]},
    {"question": "Which pet?", "header": "Pet", "multiSelect": True,
     "options": [{"label": "Cat"}, {"label": "Dog", "description": "loyal"}]},
]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    import os
    for k in list(os.environ):
        if k.startswith(("MEDIA_", "RELAY_", "TMUX")):
            monkeypatch.delenv(k, raising=False)
    for d in ("HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(d, str(tmp_path / d.lower()))
    spoken = []
    monkeypatch.setattr(H, "submit_event", lambda ev, state=None: spoken.append(ev))
    monkeypatch.setattr(H, "_ask_lead_text", lambda tp: "")
    return spoken


def test_one_part_is_rendered_with_where_it_stands():
    assert H._format_ask_question(CARD, part=0) == (
        "Question 1 of 2. Which colour? Option 1: Red (Recommended). Warm. Option 2: Blue.")
    assert H._format_ask_question(CARD, part=1) == (
        "Question 2 of 2. Which pet? Option 1: Cat. Option 2: Dog. Loyal."
        " You can pick more than one.")
    # The whole card, as a Notification still reads it.
    assert H._format_ask_question(CARD).startswith("Question 1. Which colour?")


def test_a_headless_session_hears_only_the_first_part(monkeypatch, isolated):
    monkeypatch.setenv("MEDIA_SOURCE_KIND", "headless")
    seen = {}
    monkeypatch.setattr(H, "_emit_ask", lambda ask, payload, lead="", structure=None:
                        seen.update(ask=ask) or 0)
    H._handle_pretooluse({"tool_name": "AskUserQuestion", "tool_input": CARD,
                          "session_id": "s1", "tool_use_id": "t1"})
    assert seen["ask"].startswith("Question 1 of 2. Which colour?")
    assert "Which pet?" not in seen["ask"]
    # The first part is taken: asking for it again speaks nothing.
    assert not pending_asks.claim_part("s1", "t1", 0)


def test_the_whole_card_is_read_where_no_one_can_say_a_part_was_answered(monkeypatch):
    seen = {}
    monkeypatch.setattr(H, "_emit_ask", lambda ask, payload, lead="", structure=None:
                        seen.update(ask=ask) or 0)
    H._handle_pretooluse({"tool_name": "AskUserQuestion", "tool_input": CARD,
                          "session_id": "s1", "tool_use_id": "t1"})
    assert "Which colour?" in seen["ask"] and "Which pet?" in seen["ask"]


def test_a_later_part_is_spoken_once_in_the_kept_voice(isolated):
    pending_asks.record("s1", "t1", CARD, voice="en_GB-alba", place={"tmux": "p-x"})
    assert H.speak_ask_part("s1", 1) == ""
    assert H.speak_ask_part("s1", 1) == "already_spoken"
    (ev,) = isolated
    assert ev.text.startswith("Question 2 of 2. Which pet?")
    assert ev.voice == "en_GB-alba"
    assert ev.metadata["tmux"] == "p-x" and ev.metadata["ask"] is True
    assert H.speak_ask_part("s1", 0) == "no_such_part"    # the hook's
    assert H.speak_ask_part("s1", 2) == "no_such_part"
    assert H.speak_ask_part("s1", 1, "t-other") == "not_this_question"
    assert H.speak_ask_part("nobody", 1) == "no_question"


def test_nothing_more_is_spoken_when_the_first_part_was_held(isolated):
    pending_asks.record("s1", "t1", CARD)
    pending_asks.update("s1", "t1", held=True)
    assert H.speak_ask_part("s1", 1) == "held"
    assert not isolated


def test_answering_clears_the_claims(isolated):
    pending_asks.record("s1", "t1", CARD)
    assert pending_asks.claim_part("s1", "t1", 1)
    pending_asks.clear("s1", "t1")
    pending_asks.record("s1", "t1", CARD)
    assert pending_asks.claim_part("s1", "t1", 1)
