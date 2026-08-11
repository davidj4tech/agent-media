"""The converse doorbell: a spoken question that survives not being heard.

The two properties worth defending are timing, not content. Ringing must not
delay the answer (a dozing phone holds an ssh open for the full timeout, and
the human may already be replying), and clearing must actually happen — a
notification still reading "Sam is asking" after the question expired invites
an answer to a rendezvous that no longer exists.
"""

import subprocess
import threading
import time
from pathlib import Path

import pytest

from agent_media_core.capture import doorbell


@pytest.fixture
def calls(monkeypatch):
    """Capture ssh invocations instead of running them."""
    seen = []

    def fake_run(argv, **kw):
        seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def _settle(seen, n=1, timeout=5.0):
    """ring() is async — wait for its thread to land."""
    deadline = time.monotonic() + timeout
    while len(seen) < n and time.monotonic() < deadline:
        time.sleep(0.01)
    return seen


def test_ring_puts_the_question_in_the_shade(calls):
    doorbell.ring("ship it or hold?", 90)
    argv, _ = _settle(calls)[0]
    assert argv[0] == "ssh"
    remote = argv[-1]
    assert "termux-notification" in remote
    assert "ship it or hold?" in remote
    assert "converse-reply --pending" in remote


def test_ring_does_not_block_the_answer(monkeypatch):
    """A dozed phone must cost converse nothing — the ssh runs in a thread."""
    started = threading.Event()

    def slow_run(argv, **kw):
        started.set()
        time.sleep(3)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", slow_run)
    t0 = time.monotonic()
    doorbell.ring("are you there?", 90)
    assert time.monotonic() - t0 < 0.5
    assert started.wait(2), "ring never dispatched"


def test_clear_removes_the_same_notification(calls):
    doorbell.ring("q?", 30)
    _settle(calls)
    doorbell.clear()
    remote = calls[-1][0][-1]
    assert "termux-notification-remove" in remote
    assert doorbell.NOTIFY_ID in remote
    # Same id as the ring, or it removes nothing.
    assert doorbell.NOTIFY_ID in calls[0][0][-1]


def test_clear_is_on_a_shorter_leash_than_ring(calls):
    """It tails a call that already waited minutes; it must not add twenty s."""
    doorbell.ring("q?", 30)
    _settle(calls)
    doorbell.clear()
    assert calls[-1][1]["timeout"] < calls[0][1]["timeout"]


def test_disabled_rings_nothing(calls, monkeypatch):
    monkeypatch.setenv("MEDIA_CONVERSE_NOTIFY", "0")
    doorbell.ring("q?", 30)
    doorbell.clear()
    time.sleep(0.2)
    assert calls == []


def test_empty_question_is_not_announced(calls):
    doorbell.ring("   ", 30)
    time.sleep(0.2)
    assert calls == []


# --- the mailbox drop ------------------------------------------------------
# The one path that survives the answerer not being active, so unlike the
# notification it is one-way: nothing retracts it, the text expires itself.

@pytest.fixture
def relay(monkeypatch, tmp_path):
    """Pretend relay-msg is installed, and capture how it's called."""
    fake = tmp_path / "relay-msg"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(doorbell.shutil, "which",
                        lambda n: str(fake) if n == "relay-msg" else None)
    return str(fake)


def test_post_addresses_the_answerers_box(calls, relay):
    doorbell.post("ship it or hold?", 90)
    argv, _ = _settle(calls)[0]
    assert argv[0] == relay
    assert argv[argv.index("--to") + 1] == "cece"
    # Stated, not inferred: relay-msg would otherwise label it from whichever
    # box the HOST is configured as, which breaks the reply's threading.
    assert argv[argv.index("--from") + 1] == "sam"
    assert "ship it or hold?" in argv[-1]


def test_post_says_when_it_expires(calls, relay):
    """One-way: nothing can retract it, so the text has to invalidate itself."""
    doorbell.post("still there?", 120)
    argv, _ = _settle(calls)[0]
    assert "120s" in argv[-1]
    assert "--pending" in argv[-1]


def test_post_is_configurable_and_disableable(calls, relay, monkeypatch):
    monkeypatch.setenv("MEDIA_CONVERSE_MAILBOX", "gigi")
    doorbell.post("q?", 30)
    argv, _ = _settle(calls)[0]
    assert argv[argv.index("--to") + 1] == "gigi"

    calls.clear()
    monkeypatch.setenv("MEDIA_CONVERSE_MAILBOX", "")
    doorbell.post("q?", 30)
    time.sleep(0.2)
    assert calls == []


def test_post_without_relay_msg_installed_is_a_no_op(calls, monkeypatch):
    monkeypatch.setattr(doorbell.shutil, "which", lambda n: None)
    monkeypatch.setattr(doorbell.Path, "home", staticmethod(lambda: Path("/nonexistent")))
    doorbell.post("q?", 30)
    time.sleep(0.2)
    assert calls == []


def test_post_does_not_block_the_answer(relay, monkeypatch):
    def slow_run(argv, **kw):
        time.sleep(3)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", slow_run)
    t0 = time.monotonic()
    doorbell.post("are you there?", 90)
    assert time.monotonic() - t0 < 0.5


def test_ssh_failure_is_swallowed(monkeypatch):
    """A doorbell that fails must never cost the conversation it announced."""
    def boom(argv, **kw):
        raise OSError("no route to host")

    monkeypatch.setattr(subprocess, "run", boom)
    doorbell.ring("q?", 30)
    doorbell.clear()          # must not raise
    time.sleep(0.2)
