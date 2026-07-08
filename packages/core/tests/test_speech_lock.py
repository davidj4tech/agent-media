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


def test_same_session_high_does_not_preempt(tmp_path, monkeypatch):
    """A HIGH clip must NOT preempt an in-progress NORMAL clip from the *same*
    session — within a session, priority never interrupts; siblings queue."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)
    monkeypatch.setenv("MEDIA_SPEECH_LOCK_TIMEOUT_S", "5")

    a = S._SpeechPlaybackLock()
    a.acquire(P.NORMAL, session="sess-1")  # long NORMAL reply, speaking

    b = S._SpeechPlaybackLock()

    def run_b() -> None:
        b.acquire(P.HIGH, session="sess-1")  # same session -> queues, no preempt
        time.sleep(0.05)
        b.release()

    tb = threading.Thread(target=run_b)
    tb.start()
    time.sleep(0.4)                    # let b register as a waiter

    assert a.should_yield() is False   # same-session HIGH must not interrupt
    a.release()                        # only now can b take its turn
    tb.join(timeout=3)


def test_cross_session_high_still_preempts(tmp_path, monkeypatch):
    """The cross-session preemption path is unchanged: a HIGH clip from a
    *different* session still makes the NORMAL holder yield."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)
    monkeypatch.setenv("MEDIA_SPEECH_LOCK_TIMEOUT_S", "5")

    a = S._SpeechPlaybackLock()
    a.acquire(P.NORMAL, session="sess-1")

    b = S._SpeechPlaybackLock()

    def run_b() -> None:
        b.acquire(P.HIGH, session="sess-2")  # different session -> preempts
        time.sleep(0.05)
        b.release()

    tb = threading.Thread(target=run_b)
    tb.start()

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not a.should_yield():
        time.sleep(0.02)
    assert a.should_yield() is True     # cross-session HIGH preempts as before

    a.yield_to_higher()
    tb.join(timeout=3)
    assert a._fd is not None
    a.release()


def test_same_session_admits_in_submission_order(tmp_path, monkeypatch):
    """When two same-session clips wait behind a third session, the earlier-
    submitted one wins admission even if the later one has higher priority —
    canonical order, not priority, decides within a session."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)
    monkeypatch.setenv("MEDIA_SPEECH_LOCK_TIMEOUT_S", "5")

    # A third session holds the token so our two clips both queue as waiters.
    holder = S._SpeechPlaybackLock()
    holder.acquire(P.NORMAL, session="other")

    # `early` is submitted first (NORMAL), `late` second (HIGH). Register both
    # as waiters by hand so their submission order is deterministic.
    early = S._SpeechPlaybackLock()
    early._rank = S._PRIO_RANK[P.NORMAL]
    early._session = "sess-1"
    early._seq = 100.0
    early._register()

    late = S._SpeechPlaybackLock()
    late._rank = S._PRIO_RANK[P.HIGH]
    late._session = "sess-1"
    late._seq = 200.0
    late._register()

    # The later, higher-priority clip must see it has to defer to its earlier
    # sibling; the earlier one has nothing to defer to.
    assert late._earlier_sibling_waiting() is True
    assert early._earlier_sibling_waiting() is False
    # And it is *not* a cross-session preemption (same session excluded).
    assert late._preempting_rank() <= late._rank

    early._unregister()
    late._unregister()
    holder.release()


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
