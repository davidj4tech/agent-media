"""The converse rendezvous: media-mcp waits, voice-bridge hands over.

The contract that matters is the failure mode — every error path must return
False from offer() so voice-bridge falls back to typing into the tmux pane. A
dropped transcript is a human repeating themselves; a duplicated one is
harmless.
"""

import threading
import time

import pytest

from agent_media_core.capture.rendezvous import (
    Busy, Rendezvous, offer, pending_question, question_path, socket_path,
    wait_for_question,
)


@pytest.fixture(autouse=True)
def _sock(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_CONVERSE_SOCK", str(tmp_path / "converse.sock"))


@pytest.fixture(autouse=True)
def _nobody_owns_david(tmp_path, monkeypatch):
    """And nobody else is talking to him.

    Arming refuses while another assistant holds David's input — correctly: a
    question spoken into the middle of someone else's conversation is worse
    than a late one. But the claim is a file at a fixed path under $HOME, not
    under XDG_STATE_HOME, so these tests read the live one. Whenever cece took
    the phone mic while the suite ran, `_armed` got `Claimed`, the socket never
    appeared, and fourteen tests failed as "rendezvous never armed within 10s"
    — a flake that came and went with the claim's 45s TTL and looked like
    timing, because everything about it was measured in seconds.

    Verified by pointing PATH at a claim that never expires: 14 failures, the
    same ones seen at random. This file has no opinion about claims; the ones
    that do redirect PATH exactly like this.
    """
    from agent_media_core.capture import input_claim

    monkeypatch.setattr(input_claim, "PATH", tmp_path / "input-claim.json")


def _armed(timeout_s=5.0, question=None):
    """Run a Rendezvous in a thread; returns (thread, result-dict).

    The thread says when it is armed, rather than this polling for the socket
    to appear. Watching the filesystem meant guessing how long a thread takes
    to start, and the guess was 10s — which is plenty on an idle box and not
    always enough on one at load average 10, where every failure read as
    "rendezvous never armed" and looked like a bug in the thing under test.
    An Event has no deadline to get wrong; the generous timeout below is only
    so a genuine hang fails with a sentence instead of hanging the suite.

    Armed means past __enter__, so the socket is published and listening. The
    server has not reached accept() yet, which does not matter: listen(1)
    queues a connection that arrives first.
    """
    out = {}
    armed = threading.Event()

    def serve():
        with Rendezvous(timeout_s=timeout_s, question=question) as rv:
            armed.set()
            out["reply"] = rv.wait()

    t = threading.Thread(target=serve)
    t.start()
    # If __enter__ raised — Claimed, Busy — the event never sets and the
    # traceback is already on stderr, which is the useful half.
    if not armed.wait(60.0):
        raise AssertionError("rendezvous never armed within 60s")
    return t, out


def test_offer_with_nobody_waiting_falls_back_to_injection():
    assert offer("hello") is False


def test_armed_converse_takes_the_transcript():
    t, out = _armed()
    assert offer("the feature branch") is True
    t.join()
    assert out["reply"] == "the feature branch"


def test_transcript_is_stripped():
    t, out = _armed()
    assert offer("  main  ") is True
    t.join()
    assert out["reply"] == "main"


def test_timeout_returns_none():
    with Rendezvous(timeout_s=0.3) as rv:
        assert rv.wait() is None


def test_socket_is_removed_after_use():
    t, _ = _armed()
    offer("done")
    t.join()
    assert not socket_path().exists()


def test_stale_socket_does_not_capture_a_transcript():
    """media-mcp died mid-converse: the inode outlives the listener."""
    socket_path().parent.mkdir(parents=True, exist_ok=True)
    socket_path().touch()
    assert offer("orphaned") is False


def test_stale_socket_is_reclaimed_by_a_new_converse():
    socket_path().parent.mkdir(parents=True, exist_ok=True)
    socket_path().touch()
    with Rendezvous(timeout_s=0.2) as rv:      # must not raise Busy
        assert rv.wait() is None


def test_concurrent_converse_is_refused_not_clobbered():
    t, _ = _armed(timeout_s=2.0)
    with pytest.raises(Busy):
        with Rendezvous(timeout_s=0.5):
            pass
    offer("release it")
    t.join()


def test_empty_transcript_is_not_a_reply():
    t, out = _armed(timeout_s=0.5)
    assert offer("   ") is False
    t.join()
    assert out["reply"] is None


# --- the question sidecar -------------------------------------------------
# An answerer who cannot hear the spoken question needs it in writing.

def test_armed_question_is_published_and_withdrawn():
    t, _ = _armed(question="ship it or hold?")
    q = pending_question()
    assert q["text"] == "ship it or hold?"
    assert q["timeout_s"] == 5.0
    offer("ship it")
    t.join()
    assert pending_question() is None
    assert not question_path().exists()


def test_no_question_when_nothing_is_armed():
    assert pending_question() is None


def test_orphaned_question_is_not_reported():
    """The sidecar outlived its socket — nobody is listening for an answer."""
    question_path().parent.mkdir(parents=True, exist_ok=True)
    question_path().write_text('{"text": "still there?"}')
    assert pending_question() is None


def test_converse_survives_an_unwritable_question_dir(monkeypatch):
    """A sidecar we cannot write must not cost us the rendezvous itself."""
    monkeypatch.setattr("agent_media_core.capture.rendezvous.question_path",
                        lambda: socket_path().parent / "nope" / "q.json")
    t, out = _armed(question="does this still work?")
    assert offer("yes") is True
    t.join()
    assert out["reply"] == "yes"


# --- the CLI door ---------------------------------------------------------

def _cli(*argv):
    from agent_media_core import cli
    return cli.main(["converse-reply", *argv])


def test_cli_reply_reaches_the_waiting_converse():
    t, out = _armed(question="which branch?")
    assert _cli("the feature branch") == 0
    t.join()
    assert out["reply"] == "the feature branch"


def test_cli_reply_with_nobody_waiting_is_exit_3():
    assert _cli("into the void") == 3


def test_cli_reply_to_a_stale_socket_is_exit_4():
    """Distinct from 3: something is there, but it did not take the words."""
    socket_path().parent.mkdir(parents=True, exist_ok=True)
    socket_path().touch()
    assert _cli("orphaned") == 4


def test_cli_reply_without_text_is_a_usage_error():
    t, _ = _armed(timeout_s=0.5)
    assert _cli() == 2
    t.join()


def test_wait_returns_as_soon_as_a_question_arms():
    """The answerer's own channel is slower than the arm — don't race it."""
    def arm_late():
        time.sleep(0.4)
        _armed(question="late but real")

    threading.Thread(target=arm_late, daemon=True).start()
    q = wait_for_question(5.0, poll_s=0.05)
    assert q["text"] == "late but real"
    offer("got it")


def test_wait_gives_up_and_says_so():
    t0 = time.monotonic()
    assert wait_for_question(0.3, poll_s=0.05) is None
    assert time.monotonic() - t0 >= 0.3


def test_cli_pending_wait_outlasts_a_late_arm(capsys):
    def arm_late():
        time.sleep(0.4)
        _armed(question="which branch?")

    threading.Thread(target=arm_late, daemon=True).start()
    assert _cli("--pending", "--wait", "5") == 0
    assert capsys.readouterr().out.strip() == "which branch?"
    offer("main")


def test_cli_pending_prints_the_question(capsys):
    t, _ = _armed(question="ship it or hold?")
    assert _cli("--pending") == 0
    assert capsys.readouterr().out.strip() == "ship it or hold?"
    offer("hold")
    t.join()
    assert _cli("--pending") == 3
