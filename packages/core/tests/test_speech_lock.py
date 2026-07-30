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


def test_same_session_urgent_barges_in(tmp_path, monkeypatch):
    """URGENT is the same-session exception: it interrupts the in-progress clip
    at the next boundary, then (default) the interrupted clip resumes."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)
    monkeypatch.setenv("MEDIA_SPEECH_LOCK_TIMEOUT_S", "5")

    a = S._SpeechPlaybackLock()
    a.acquire(P.NORMAL, session="sess-1")  # long reply speaking

    b = S._SpeechPlaybackLock()
    held: dict = {}

    def run_b() -> None:
        b.acquire(P.URGENT, session="sess-1")  # same session, but URGENT -> barge in
        held["fd"] = b._fd
        time.sleep(0.1)
        b.release()

    tb = threading.Thread(target=run_b)
    tb.start()

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not a.should_yield():
        time.sleep(0.02)
    assert a.should_yield() is True     # same-session URGENT triggers a yield

    a.yield_to_higher()
    tb.join(timeout=3)
    assert held.get("fd") is not None   # the URGENT clip actually got the token
    assert a._fd is not None            # and the interrupted reply resumed (kept)
    a.release()


def test_urgent_ignores_earlier_sibling_at_admission(tmp_path, monkeypatch):
    """An URGENT clip jumps its session's queue: it does not defer to an
    earlier-submitted sibling the way an ordinary clip would."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)

    # An earlier same-session sibling sits in the waiter registry.
    early = S._SpeechPlaybackLock()
    early._rank = S._PRIO_RANK[P.NORMAL]
    early._session = "sess-1"
    early._seq = 100.0
    early._register()

    urgent = S._SpeechPlaybackLock()
    urgent._rank = S._PRIO_RANK[P.URGENT]
    urgent._session = "sess-1"
    urgent._seq = 200.0  # submitted later, yet must not defer
    assert urgent._is_urgent() is True
    assert urgent._earlier_sibling_waiting() is True   # the earlier one exists...
    # ...but URGENT is exempt, so acquisition proceeds rather than handing back.
    urgent.acquire(P.URGENT, session="sess-1")
    assert urgent._fd is not None
    urgent.release()
    early._unregister()


def test_supersede_marker_aborts_earlier_same_session(tmp_path, monkeypatch):
    """A supersede barge-in drops the same-session clips it interrupts/precedes
    (earlier seq) — but not itself, later clips, or other sessions."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)

    older = S._SpeechPlaybackLock()
    older._session, older._seq = "sess-1", 100.0
    assert older.should_abort() is False   # nothing has superseded yet

    sup = S._SpeechPlaybackLock()
    sup._session, sup._seq = "sess-1", 200.0
    sup._rank = S._PRIO_RANK[P.URGENT]
    sup._supersede = True
    sup._mark_supersede()

    assert older.should_abort() is True    # earlier clip -> dropped
    assert sup.should_abort() is False     # the superseding clip keeps playing

    later = S._SpeechPlaybackLock()
    later._session, later._seq = "sess-1", 300.0
    assert later.should_abort() is False   # submitted after the supersede

    other = S._SpeechPlaybackLock()
    other._session, other._seq = "sess-2", 100.0
    assert other.should_abort() is False   # different session untouched


def test_supersede_requires_urgent_and_session(tmp_path, monkeypatch):
    """supersede only bites for an URGENT clip with a real session id — a NORMAL
    (or session-less) clip tagged supersede publishes no marker."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)

    older = S._SpeechPlaybackLock()
    older._session, older._seq = "sess-1", 100.0

    n = S._SpeechPlaybackLock()
    n.acquire(P.NORMAL, session="sess-1", supersede=True)  # NORMAL -> ignored
    assert n._supersede is False
    assert older.should_abort() is False
    n.release()


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

def test_paused_holder_not_overtaken(tmp_path, monkeypatch):
    """A deliberately-paused holder (popup Space) must NOT be overtaken by a
    waiter, and its now_playing name must survive an incoming sibling.

    The holder stops advancing its clip uri while paused, so the progress-aware
    give-up can't see it's healthy — the pause exemption is what keeps the
    waiter from timing out, taking the token unserialized, and clobbering the
    shared speech now_playing row with its own session.
    """
    from agent_media_core.state import StateStore

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)
    monkeypatch.setenv("MEDIA_SPEECH_LOCK_TIMEOUT_S", "0.3")

    store = StateStore()
    # Paused holder's now_playing: live_pause mirrored into extras (the same
    # signal the playlist/remote loop records), so no real broker is needed.
    store.set_now_playing(
        "speech", uri="clip-a-000", started_at=time.time(),
        target="local", extras={"source_session": "sess-A", "live_pause": True})

    a = S._SpeechPlaybackLock()
    a.acquire(P.NORMAL, session="sess-A")
    assert a._fd is not None

    b = S._SpeechPlaybackLock()
    t0 = time.monotonic()
    b.acquire(P.NORMAL, session="sess-B")
    waited = time.monotonic() - t0
    # Paused holder is exempt from the give-up: the waiter keeps waiting well
    # past the timeout instead of overtaking.
    assert b._fd is None
    assert waited >= 0.6       # did NOT bail at the 0.3s deadline

    # The paused holder's name is intact — no sibling clobbered now_playing.
    np = store.get_now_playing("speech")
    assert np is not None
    assert (np.get("extras") or {}).get("source_session") == "sess-A"

    a.release()


def test_wedged_holder_still_times_out(tmp_path, monkeypatch):
    """A genuinely wedged holder (not paused, not advancing) still times out so
    a waiter proceeds unserialized rather than being lost forever."""
    from agent_media_core.state import StateStore

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)
    monkeypatch.setenv("MEDIA_SPEECH_LOCK_TIMEOUT_S", "0.3")

    store = StateStore()
    # Not paused: live_pause False, uri never advances -> wedged.
    store.set_now_playing(
        "speech", uri="clip-a-000", started_at=time.time(),
        target="local", extras={"source_session": "sess-A", "live_pause": False})

    a = S._SpeechPlaybackLock()
    a.acquire(P.NORMAL, session="sess-A")

    b = S._SpeechPlaybackLock()
    t0 = time.monotonic()
    b.acquire(P.NORMAL, session="sess-B")
    waited = time.monotonic() - t0
    assert b._fd is None            # gave up (proceeds unserialized)
    assert 0.3 <= waited < 1.5      # bailed at the deadline, didn't hang

    a.release()


def test_acquire_orders_by_submission_not_acquire_time(tmp_path, monkeypatch):
    """Same-session order follows the caller's submission time, not the moment
    the token is asked for.

    Rendering happens before acquire() and takes longer for a longer reply, so
    a short follow-up submitted *after* a long reply can reach acquire() first.
    With `seq` carrying submission time it still recognises the long reply as
    its elder; stamping seq at acquire() time is what let the pair be heard
    back to front.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)
    monkeypatch.setenv("MEDIA_SPEECH_LOCK_TIMEOUT_S", "0.3")

    # The long reply: submitted at t=100, still rendering, so it reaches
    # acquire() *after* the short one — but registers with its own seq.
    early = S._SpeechPlaybackLock()
    early._rank = S._PRIO_RANK[P.NORMAL]
    early._session = "sess-1"
    early._seq = 100.0
    early._register()

    # The short reply: submitted at t=103, rendered fast, acquiring now. It
    # takes the seq it was given, not "now", so it sees the elder sibling and
    # stands aside (here nobody ever claims the token, so it eventually gives
    # up and speaks — bounded, not a spin).
    late = S._SpeechPlaybackLock()
    t0 = time.monotonic()
    late.acquire(P.NORMAL, session="sess-1", seq=103.0)
    assert late._seq == 103.0
    assert late._earlier_sibling_waiting() is True
    assert time.monotonic() - t0 >= 0.3     # it did defer, rather than barging

    late.release()
    early._unregister()


def test_pending_sibling_holds_its_place_while_rendering(tmp_path, monkeypatch):
    """A sibling that has only *announced* (still rendering its clips) still
    counts as an earlier sibling — that's what stops a faster-rendering later
    reply from speaking first."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)

    now = time.time()
    early = S._SpeechPlaybackLock()
    early.announce(P.NORMAL, session="sess-1", seq=now - 3)

    late = S._SpeechPlaybackLock()
    late._session, late._seq = "sess-1", now
    assert late._earlier_sibling_waiting() is True

    # Pending entries are same-session ordering only: they neither preempt
    # another session nor make an in-progress speaker yield.
    other = S._SpeechPlaybackLock()
    other._session, other._seq = "sess-2", now
    other._rank = S._PRIO_RANK[P.NORMAL]
    assert other._preempting_rank() == -1
    assert other._earlier_sibling_waiting() is False

    # Once the render finishes and it acquires for real, it's a normal waiter.
    early.acquire(P.NORMAL, session="sess-1", seq=now - 3)
    assert early._fd is not None
    assert early._pending is False
    early.release()
    assert late._earlier_sibling_waiting() is False   # entry gone on release


def test_pending_sibling_expires(tmp_path, monkeypatch):
    """A wedged/abandoned render must not mute the rest of its session: a
    pending entry stops counting after MEDIA_SPEECH_PENDING_TTL_S."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)
    monkeypatch.setenv("MEDIA_SPEECH_PENDING_TTL_S", "0.2")

    now = time.time()
    stuck = S._SpeechPlaybackLock()
    stuck.announce(P.NORMAL, session="sess-1", seq=now)

    late = S._SpeechPlaybackLock()
    late._session, late._seq = "sess-1", now + 1
    assert late._earlier_sibling_waiting() is True
    time.sleep(0.3)
    assert late._earlier_sibling_waiting() is False   # TTL expired; go ahead

    stuck.release()


def test_pending_urgent_sibling_does_not_trigger_yield(tmp_path, monkeypatch):
    """An URGENT sibling that's still rendering has nothing to hand over to, so
    the current speaker keeps going until it actually contends."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_SPEECH_SERIALIZE", raising=False)

    speaking = S._SpeechPlaybackLock()
    speaking.acquire(P.NORMAL, session="sess-1", seq=time.time())
    assert speaking._fd is not None

    pending_urgent = S._SpeechPlaybackLock()
    pending_urgent.announce(P.URGENT, session="sess-1", seq=time.time())
    assert speaking.should_yield() is False

    pending_urgent.release()
    speaking.release()
