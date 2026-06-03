"""Cross-process serialization of speech playback (_SpeechPlaybackLock).

Concurrent sessions share one sink-speech broker + one Snapcast stream; the
lock makes them play one at a time instead of clobbering each other. flock
contends across separate open descriptions even within a single process, so
two lock instances here exercise the same contention two processes would.
"""

import threading
import time

from agent_media_core.intake import submit as S
from agent_media_core.types import Priority as P


def test_lock_serializes_and_releases(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)
    monkeypatch.setenv("MEDIA_SPEECH_LOCK_TIMEOUT_S", "0.3")

    a = S._SpeechPlaybackLock()
    a.acquire()
    assert a._fd is not None  # first speaker holds it

    b = S._SpeechPlaybackLock()
    t0 = time.monotonic()
    b.acquire()
    waited = time.monotonic() - t0
    assert b._fd is None       # couldn't acquire while a holds it
    assert waited >= 0.3       # waited out the timeout, then proceeded

    a.release()
    b.acquire()
    assert b._fd is not None   # now free
    b.release()


def test_lock_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MEDIA_SPEECH_SERIALIZE", "0")

    a = S._SpeechPlaybackLock()
    a.acquire()
    assert a._fd is None       # no real lock taken
    b = S._SpeechPlaybackLock()
    b.acquire()
    assert b._fd is None       # second one is unaffected too
    a.release()
    b.release()


def test_lock_context_manager_releases(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)
    monkeypatch.setenv("MEDIA_SPEECH_LOCK_TIMEOUT_S", "0.3")

    with S._SpeechPlaybackLock() as held:
        assert held._fd is not None

    # exiting the with-block released it, so a fresh acquire succeeds
    after = S._SpeechPlaybackLock()
    after.acquire()
    assert after._fd is not None
    after.release()


def test_high_priority_preempts_normal(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)
    monkeypatch.setenv("MEDIA_SPEECH_LOCK_TIMEOUT_S", "5")

    a = S._SpeechPlaybackLock()
    a.acquire(P.NORMAL)
    assert a._fd is not None  # NORMAL response is speaking

    b = S._SpeechPlaybackLock()
    held: dict = {}

    def run_b() -> None:
        b.acquire(P.HIGH)       # blocks until `a` steps aside
        held["fd"] = b._fd
        time.sleep(0.2)
        b.release()

    tb = threading.Thread(target=run_b)
    tb.start()

    # `a` should notice the higher-priority waiter and offer to yield.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not a.should_yield():
        time.sleep(0.02)
    assert a.should_yield() is True

    a.yield_to_higher()         # hand off to b, block, then re-take after b
    tb.join(timeout=3)
    assert held.get("fd") is not None  # the HIGH clip actually got the token
    assert a._fd is not None           # and the NORMAL response resumed holding
    a.release()


def test_equal_priority_does_not_preempt(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)
    monkeypatch.setenv("MEDIA_SPEECH_LOCK_TIMEOUT_S", "5")

    a = S._SpeechPlaybackLock()
    a.acquire(P.NORMAL)

    b = S._SpeechPlaybackLock()

    def run_b() -> None:
        b.acquire(P.NORMAL)     # queues behind a, never preempts
        time.sleep(0.05)
        b.release()

    tb = threading.Thread(target=run_b)
    tb.start()
    time.sleep(0.4)             # give b time to register as a waiter

    assert a.should_yield() is False  # equal priority must not preempt
    a.release()                 # now b can take its turn
    tb.join(timeout=3)


def test_low_priority_skips_when_busy(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)
    monkeypatch.setenv("MEDIA_SPEECH_LOCK_TIMEOUT_S", "5")

    a = S._SpeechPlaybackLock()
    a.acquire(P.NORMAL)

    low = S._SpeechPlaybackLock()
    t0 = time.monotonic()
    low.acquire(P.LOW)          # anything playing -> skip, don't queue
    assert low._fd is None
    assert time.monotonic() - t0 < 1.0  # returned immediately, no wait

    a.release()
    # token free now, so a LOW clip can take it
    low2 = S._SpeechPlaybackLock()
    low2.acquire(P.LOW)
    assert low2._fd is not None
    low2.release()
