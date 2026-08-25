"""`media ask` and the endpoint the phone reaches it through.

The command's job is to be exact about *which* of the several ways an ask can
fail to arrive happened, because the surfaces above it can only say something
useful if they are told apart: no live conversation (3), typed but not accepted
(4), nothing asked at all (1).
"""

import json

import pytest

from agent_media_core import conversation as C
from agent_media_core.cli import main
from agent_media_core.entrypoints import share_control as control


@pytest.fixture()
def live(monkeypatch):
    """One live conversation, no tmux and no history involved."""
    conv = C.Conversation(session="s1", window="deploy", pane="%1", tmux="t",
                          at=1000.0, text="what I said before")
    monkeypatch.setattr(C, "resolve", lambda session="", **kw:
                        conv if session in ("", "s1") else None)
    monkeypatch.setattr(C, "liveness", lambda c, **kw:
                        C.Liveness(True, "deploy is listening") if c
                        else C.Liveness(False, "no conversation has spoken here yet"))
    sent = []
    monkeypatch.setattr(C, "deliver",
                        lambda c, line, **kw: sent.append(line) or True)
    monkeypatch.setattr("agent_media_core.cli._ask_context",
                        lambda ch: f"I'm listening to {ch}: Blue [2:00]")
    return sent


@pytest.fixture()
def closed(monkeypatch):
    monkeypatch.setattr(C, "resolve", lambda session="", **kw: None)
    monkeypatch.setattr(C, "liveness", lambda c, **kw:
                        C.Liveness(False, "deploy has closed"))
    monkeypatch.setattr("agent_media_core.cli._ask_context", lambda ch: "")


# ---- the command -----------------------------------------------------------

def test_asking_a_live_conversation(live, capsys):
    assert main(["ask", "who wrote this?"]) == 0
    assert live == ["[media ask] I'm listening to speech: Blue [2:00] — "
                    "who wrote this?"]
    assert "asked deploy" in capsys.readouterr().out


def test_the_channels_context_is_the_one_prepended(live):
    main(["ask", "--channel", "book", "who wrote this?"])
    assert "listening to book" in live[0]


def test_context_can_be_left_off(live):
    main(["ask", "--no-context", "who wrote this?"])
    assert live == ["[media ask] who wrote this?"]


def test_no_live_conversation_is_exit_3_not_1(closed, capsys):
    """It is an answer, not a failure: the caller wants to say 'that
    conversation has closed' rather than 'error'."""
    assert main(["ask", "who wrote this?"]) == 3
    assert "deploy has closed" in capsys.readouterr().err


def test_nothing_asked_is_exit_1(live):
    assert main(["ask", "   "]) == 1


def test_typed_but_not_accepted_is_exit_4(live, monkeypatch, capsys):
    monkeypatch.setattr(C, "deliver", lambda *a, **k: False)
    assert main(["ask", "who wrote this?"]) == 4
    assert "did not take it" in capsys.readouterr().err


def test_dry_run_types_nothing(live, capsys):
    assert main(["ask", "--dry-run", "why?"]) == 0
    assert live == []
    assert capsys.readouterr().out.strip().endswith("— why?")


def test_status_says_who_would_be_asked(live, capsys):
    assert main(["ask", "--status"]) == 0
    out = capsys.readouterr().out
    assert "deploy is listening" in out and "what I said before" in out


def test_status_is_exit_3_when_nobody_is_listening(closed):
    assert main(["ask", "--status"]) == 3


def test_status_json_carries_the_conversation(live, capsys):
    main(["ask", "--status", "--json"])
    got = json.loads(capsys.readouterr().out)
    assert got["session"] == "s1" and got["pane"] == "%1"
    assert got["live"] is True and got["label"] == "deploy"


def test_a_named_conversation_that_never_spoke_says_so(live, capsys):
    main(["ask", "--status", "--session", "nope", "--json"])
    got = json.loads(capsys.readouterr().out)
    assert got["reason"] == "that conversation has not spoken here"


def test_json_reports_a_refusal_without_a_traceback(closed, capsys):
    assert main(["ask", "--json", "why?"]) == 3
    got = json.loads(capsys.readouterr().out)
    assert got["asked"] is False and got["live"] is False


def test_the_tag_is_the_callers(live):
    main(["ask", "--via", "the phone", "why?"])
    assert live[0].startswith("[the phone] ")


# ---- the endpoint's half ---------------------------------------------------

def test_ask_runs_local_when_this_host_is_the_origin(monkeypatch):
    monkeypatch.setattr(control, "_origin_host", lambda: None)
    seen = []
    got = control.ask("why?", runner=lambda argv: seen.append(argv) or 0)
    assert got["asked"] is True and seen[0][0] == "ask"


def test_a_refusal_comes_back_as_a_sentence(monkeypatch):
    monkeypatch.setattr(control, "_origin_host", lambda: None)
    got = control.ask("why?", runner=lambda argv: 3)
    assert got["asked"] is False
    assert got["reason"] == "no conversation is listening"


def test_typed_but_not_accepted_is_distinguishable(monkeypatch):
    monkeypatch.setattr(control, "_origin_host", lambda: None)
    got = control.ask("why?", runner=lambda argv: 4)
    assert got["asked"] is False and got["live"] is True


def test_an_empty_question_never_leaves_the_house(monkeypatch):
    monkeypatch.setattr(control, "_origin_host",
                        lambda: pytest.fail("should not have dialled"))
    assert control.ask("  ")["asked"] is False


def test_the_ask_is_put_to_the_origin(monkeypatch):
    """A conversation is a pane on the hub and a transcript beside it. A render
    host has neither."""
    monkeypatch.setattr(control, "_origin_host", lambda: "red5")
    seen = []

    def fake(argv, timeout=20.0):
        seen.append(argv)
        return json.dumps({"live": True, "asked": True, "reason": "deploy"})

    monkeypatch.setattr(control, "_ask_origin", fake)
    assert control.ask("why?")["asked"] is True
    assert seen[0][0] == "ask" and seen[0][-1] == "why?"


def test_an_unreachable_hub_is_not_a_refusal(monkeypatch):
    """'The hub is asleep' and 'that conversation has closed' are different
    situations, and only one of them is worth retrying."""
    monkeypatch.setattr(control, "_origin_host", lambda: "red5")
    monkeypatch.setattr(control, "_ask_origin", lambda argv, timeout=20.0: None)
    got = control.ask("why?")
    assert got["reachable"] is False and got["asked"] is False


def test_nonsense_from_the_hub_is_not_a_traceback(monkeypatch):
    monkeypatch.setattr(control, "_origin_host", lambda: "red5")
    monkeypatch.setattr(control, "_ask_origin", lambda argv, timeout=20.0: "{{{")
    assert control.ask("why?")["reachable"] is False


def test_status_from_the_origin(monkeypatch):
    monkeypatch.setattr(control, "_origin_host", lambda: "red5")
    monkeypatch.setattr(control, "_ask_origin", lambda argv, timeout=20.0:
                        json.dumps({"live": True, "label": "deploy"}))
    got = control.ask_status()
    assert got["live"] is True and got["reachable"] is True


def test_status_when_the_hub_is_away(monkeypatch):
    monkeypatch.setattr(control, "_origin_host", lambda: "red5")
    monkeypatch.setattr(control, "_ask_origin", lambda argv, timeout=20.0: None)
    got = control.ask_status()
    assert got["live"] is False and got["reachable"] is False


def test_the_question_reaches_the_origin_intact(monkeypatch):
    """`ssh host a b c` hands the remote shell `a b c`, so an unquoted question
    arrives as several arguments or not at all."""
    monkeypatch.setattr(control, "_origin_host", lambda: "red5")
    seen = {}

    class R:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def run(argv, **kw):
        seen["argv"] = argv
        return R()

    monkeypatch.setattr(control.subprocess, "run", run)
    control._ask_origin(["ask", "what's this; rm -rf /"])
    assert seen["argv"][-1] == "media ask 'what'\"'\"'s this; rm -rf /'"
