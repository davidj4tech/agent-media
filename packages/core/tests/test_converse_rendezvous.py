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
    Busy, Rendezvous, offer, socket_path,
)


@pytest.fixture(autouse=True)
def _sock(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_CONVERSE_SOCK", str(tmp_path / "converse.sock"))


def _armed(timeout_s=5.0):
    """Run a Rendezvous in a thread; returns (thread, result-dict)."""
    out = {}

    def serve():
        with Rendezvous(timeout_s=timeout_s) as rv:
            out["reply"] = rv.wait()

    t = threading.Thread(target=serve)
    t.start()
    for _ in range(50):                     # wait for the bind
        if socket_path().exists():
            break
        time.sleep(0.02)
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
