"""Which conversation is this, and is it still going.

The module under test adds no state of its own — it reads the tags speech
history already carries — so most of what is worth pinning down is about
*refusing*: a pane that has been recycled, a conversation that has gone quiet,
a line that was typed and never accepted. Each of those is a different answer,
and the tests keep them different.
"""

import json
import time

import pytest

from agent_media_core import conversation as C


def _row(session="s1", pane="%1", window="w", at=None, text="hi", tmux="t"):
    return {"session": session, "pane": pane, "window": window, "tmux": tmux,
            "at": time.time() if at is None else at, "text": text}


# ---- resolving -------------------------------------------------------------

def test_newest_conversation_wins():
    rows = [_row("new", at=200), _row("old", at=100)]
    assert C.resolve(rows=rows).session == "new"


def test_rows_without_a_session_are_not_conversations():
    """The newest speech is usually cron — org reminders speak through the same
    lane and belong to nobody."""
    rows = [_row(session="", text="Org reminder: moon"), _row("real", at=1)]
    assert C.resolve(rows=rows).session == "real"


def test_one_entry_per_conversation_newest_first():
    rows = [_row("a", at=300), _row("b", at=200), _row("a", at=100)]
    assert [c.session for c in C.rows_to_conversations(rows)] == ["a", "b"]


def test_a_conversation_keeps_the_newest_rows_details():
    rows = [_row("a", pane="%9", at=300, text="latest"), _row("a", pane="%1", at=1)]
    conv = C.resolve(rows=rows)
    assert (conv.pane, conv.text) == ("%9", "latest")


def test_resolve_by_name():
    rows = [_row("a", at=2), _row("b", at=1)]
    assert C.resolve("b", rows=rows).session == "b"


def test_naming_an_unknown_conversation_finds_nothing():
    assert C.resolve("nope", rows=[_row("a")]) is None


def test_no_rows_at_all():
    assert C.resolve(rows=[]) is None


def test_label_prefers_the_window():
    assert C.resolve(rows=[_row(window="deploy")]).label == "deploy"


def test_label_falls_back_to_a_session_stub():
    """Clips predating source_window are still distinct conversations, and a
    list where they are all "(untagged)" cannot be navigated."""
    assert C.resolve(rows=[_row("abcd1234", window="")]).label == "…1234"


def test_label_of_nothing():
    assert C.Conversation(session="").label == "(untagged)"


# ---- liveness --------------------------------------------------------------

class _Store:
    def __init__(self, owner):
        self.owner = owner

    def session_for_pane(self, pane):
        return self.owner


@pytest.fixture()
def alive(monkeypatch):
    monkeypatch.setattr(C, "pane_alive", lambda pane: True)


def test_live_when_the_pane_is_there_and_still_ours(alive):
    conv = C.resolve(rows=[_row("s1", pane="%1")])
    assert C.liveness(conv, store=_Store("s1")).live is True


def test_a_recycled_pane_is_not_this_conversation(alive):
    """The pane→session direction is a heuristic: one observed pane had carried
    twelve conversations. Verifying the other way is what stops a question
    being typed into somebody else's."""
    conv = C.resolve(rows=[_row("s1", pane="%1")])
    live = C.liveness(conv, store=_Store("s2"))
    assert live.live is False and "%1" in live.reason


def test_a_closed_pane_says_so(monkeypatch):
    monkeypatch.setattr(C, "pane_alive", lambda pane: False)
    conv = C.resolve(rows=[_row("s1", window="deploy")])
    live = C.liveness(conv, store=_Store("s1"))
    assert live.live is False and live.reason == "deploy has closed"


def test_a_quiet_conversation_is_not_ongoing(alive):
    conv = C.resolve(rows=[_row("s1", at=time.time() - 7200)])
    live = C.liveness(conv, store=_Store("s1"))
    assert live.live is False and "quiet for 120 minutes" in live.reason


def test_the_quiet_window_is_a_parameter(alive):
    conv = C.resolve(rows=[_row("s1", at=time.time() - 60)])
    assert C.liveness(conv, store=_Store("s1"), live_s=10.0).live is False
    assert C.liveness(conv, store=_Store("s1"), live_s=600.0).live is True


def test_no_conversation_at_all():
    assert C.liveness(None).live is False


def test_a_conversation_with_no_pane():
    conv = C.Conversation(session="s1", window="w", at=time.time())
    assert "not recorded against a pane" in C.liveness(conv).reason


def test_an_unreadable_history_is_not_a_traceback(alive):
    class Broken:
        def session_for_pane(self, pane):
            raise RuntimeError("db is gone")

    conv = C.resolve(rows=[_row()])
    assert C.liveness(conv, store=Broken()).live is False


def test_liveness_is_truthy(alive):
    conv = C.resolve(rows=[_row("s1")])
    assert bool(C.liveness(conv, store=_Store("s1"))) is True


# ---- the transcript --------------------------------------------------------

@pytest.fixture()
def claude_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    proj = tmp_path / "projects" / "-home-someone-thing"
    proj.mkdir(parents=True)
    return proj


def test_a_transcript_is_found_by_session_alone(claude_home):
    """Transcripts are named for the session, so which project holds it never
    has to be worked out — which is why the needle search here is exact where
    tmux-relay's has to grep a whole directory."""
    (claude_home / "abc.jsonl").write_text("{}")
    assert C.transcript("abc").name == "abc.jsonl"


def test_no_transcript_for_a_session_that_never_ran(claude_home):
    assert C.transcript("abc") is None


def test_a_session_id_may_not_be_a_path(claude_home):
    (claude_home / "abc.jsonl").write_text("{}")
    assert C.transcript("../abc") is None
    assert C.transcript("") is None


def test_landed_finds_the_line(claude_home):
    (claude_home / "abc.jsonl").write_text(
        json.dumps({"role": "user", "text": "[media ask] what is playing"}))
    assert C.landed("abc", "[media ask] what is playing", timeout=0.0) is True


def test_landed_matches_through_json_escaping(claude_home):
    """The transcript is JSONL, so a question with a quote in it is on disk
    escaped, and a literal search for the plain form misses it."""
    line = 'is "this" the one?'
    (claude_home / "abc.jsonl").write_text(json.dumps({"text": line}))
    assert C.landed("abc", line, timeout=0.0) is True


def test_landed_is_false_when_the_enter_was_swallowed(claude_home):
    (claude_home / "abc.jsonl").write_text(json.dumps({"text": "something else"}))
    assert C.landed("abc", "the question", timeout=0.0) is False


def test_landed_waits_for_a_session_that_has_no_transcript_yet(claude_home):
    """Checking once up front would report 'did not land' for a line that lands
    a second later — the false negative this exists to avoid."""
    calls = []
    real = C.transcript

    def slow(session):
        calls.append(session)
        if len(calls) < 3:
            return None
        (claude_home / "abc.jsonl").write_text("the question")
        return real(session)

    C.transcript, saved = slow, C.transcript
    try:
        assert C.landed("abc", "the question", timeout=5.0) is True
    finally:
        C.transcript = saved
    assert len(calls) >= 3


def test_landed_needs_something_to_look_for(claude_home):
    assert C.landed("abc", "   ", timeout=0.0) is False


# ---- composing -------------------------------------------------------------

def test_the_line_is_tagged():
    assert C.compose("what is this?").startswith("[media ask] ")


def test_context_comes_before_the_question():
    line = C.compose("who wrote it?", "I'm listening to music: Blue [2:00]")
    assert line == "[media ask] I'm listening to music: Blue [2:00] — who wrote it?"


def test_no_context_is_just_the_question():
    assert C.compose("why?") == "[media ask] why?"


def test_the_line_never_contains_a_newline():
    """A literal newline typed into Claude Code submits, so a two-line ask would
    send half a question and leave the rest in the box."""
    line = C.compose("first\nsecond", "a\nb")
    assert "\n" not in line and "first second" in line


def test_nothing_to_compose():
    assert C.compose("", "") == ""


def test_the_tag_is_the_callers():
    assert C.compose("why?", via="the phone").startswith("[the phone] ")


# ---- delivery --------------------------------------------------------------

@pytest.fixture()
def tmux(monkeypatch):
    calls = []

    class Result:
        returncode = 0

    def run(argv, **kw):
        calls.append(argv)
        return Result()

    monkeypatch.setattr(C.subprocess, "run", run)
    return calls


def test_delivery_types_then_submits(tmux, monkeypatch):
    monkeypatch.setattr(C, "landed", lambda *a, **k: True)
    conv = C.Conversation(session="s1", pane="%1")
    assert C.deliver(conv, "[media ask] why?") is True
    assert tmux[0][:5] == ["tmux", "send-keys", "-t", "%1", "-l"]
    assert tmux[1][-1] == "Enter"


def test_the_question_is_typed_literally(tmux, monkeypatch):
    """`-l` matters: a question mentioning Enter or C-c is text, not keys."""
    monkeypatch.setattr(C, "landed", lambda *a, **k: True)
    C.deliver(C.Conversation(session="s1", pane="%1"), "does Enter work?")
    assert "-l" in tmux[0] and tmux[0][-1] == "does Enter work?"


def test_typed_but_not_accepted_is_not_delivered(tmux, monkeypatch):
    """send-keys reports that the key reached the pane, which is not the same
    as Claude Code accepting it."""
    monkeypatch.setattr(C, "landed", lambda *a, **k: False)
    assert C.deliver(C.Conversation(session="s1", pane="%1"), "why?") is False


def test_verification_can_be_waived(tmux, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not have looked")

    monkeypatch.setattr(C, "landed", boom)
    assert C.deliver(C.Conversation(session="s1", pane="%1"), "why?",
                     verify=False) is True


def test_a_failed_send_is_not_delivery(monkeypatch):
    class Bad:
        returncode = 1

    monkeypatch.setattr(C.subprocess, "run", lambda *a, **k: Bad())
    assert C.deliver(C.Conversation(session="s1", pane="%1"), "why?") is False


def test_tmux_missing_entirely_is_not_a_traceback(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(C.subprocess, "run", boom)
    assert C.deliver(C.Conversation(session="s1", pane="%1"), "why?") is False
    assert C.pane_alive("%1") is False


def test_nothing_to_deliver(tmux):
    assert C.deliver(C.Conversation(session="s1", pane="%1"), "  ") is False


def test_a_conversation_with_no_pane_cannot_be_typed_into(tmux):
    assert C.deliver(C.Conversation(session="s1", pane=""), "why?") is False


def test_pane_alive_wants_the_same_id_back(monkeypatch):
    class R:
        returncode = 0
        stdout = "%1\n"

    monkeypatch.setattr(C.subprocess, "run", lambda *a, **k: R())
    assert C.pane_alive("%1") is True
    assert C.pane_alive("%2") is False


def test_an_unexpanded_format_is_not_a_pane(monkeypatch):
    monkeypatch.setattr(C.subprocess, "run",
                        lambda *a, **k: pytest.fail("should not have asked"))
    assert C.pane_alive("#{pane_id}") is False
