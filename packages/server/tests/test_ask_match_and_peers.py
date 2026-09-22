"""Two things a phone showed on 2026-09-22.

A two-question card read "answer it at the desk" although the hook had saved
both questions: on a 34-column pane only a fragment of the question was on
screen, and the match wanted its opening words. And another session's
message showed as the listener's bubble, raw tags and all.
"""

from agent_media_server import asks, transcript

HOOK = {"tool_use_id": "t1", "questions": [
    {"question": "How much should Codex chats started from the app do without asking?",
     "options": [{"label": "Auto review (Recommended)"}, {"label": "Never ask, sandboxed"},
                 {"label": "Fully hands-off"}]},
    {"question": "Where should that setting apply?",
     "options": [{"label": "App-started chats (Recommended)"}, {"label": "Everywhere"}]}]}


def scr(question, labels, review=False):
    return {"question": asks._norm(question), "review": review, "reviewed": [],
            "options": [{"label": l} for l in labels]}


def test_a_fragment_from_the_middle_matches():
    assert asks._match(scr("chats started from the app do", []), HOOK) == 0


def test_options_match_when_the_words_scrolled_away():
    assert asks._match(scr("", ["App-started chats (Recommended)", "Everywhere"]), HOOK) == 1


def test_truncated_labels_still_match():
    assert asks._match(scr("", ["Auto review (Recomm", "Never ask, sand", "Fully hands"]), HOOK) == 0


def test_one_pending_question_with_nothing_on_screen_is_the_one_up():
    one = {"questions": [HOOK["questions"][0]]}
    assert asks._match(scr("", []), one) == 0


def test_a_stale_single_copy_loses_to_what_is_on_screen():
    one = {"questions": [HOOK["questions"][0]]}
    assert asks._match(scr("Which fruits?", ["Apple", "Pear"]), one) is None


def test_two_questions_and_nothing_to_go_on_is_none():
    assert asks._match(scr("zzz", ["x"]), HOOK) is None


def test_a_cross_session_message_is_marked_and_untagged():
    m = transcript._PEER.match('<cross-session-message from="uds:/x.sock" from-name="agent-media-71" '
                               'from-mode="bypass">\nNotes follow-up: merged.\n</cross-session-message>\n\nThis came from another Claude session')
    assert m and m.group("body").strip() == "Notes follow-up: merged."
    assert transcript._PEER_NAME.search(m.group("attrs")).group(1) == "agent-media-71"
