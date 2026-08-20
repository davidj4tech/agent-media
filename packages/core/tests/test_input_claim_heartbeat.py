"""call_guard's input-claim heartbeat: tell red5 the mic is spoken for.

The property that matters is that this can never hurt the thing it rides on.
The duck is a real, working feature; the claim is an optimisation on top of it,
so every failure here must be absorbed. A missed claim costs an overlap, which
is the status quo. A claim that could wedge the guard would be a bad trade.
"""

import json
import threading
import time

import pytest

from agent_media_core import call_guard as cg


class _Recorder:
    """Stands in for urlopen, capturing each POST."""

    def __init__(self, fail: bool = False, status: int = 200) -> None:
        self.calls: list[dict] = []
        self.fail = fail
        self.status = status
        self.seen = threading.Event()

    def __call__(self, req, timeout=None):
        self.calls.append({
            "url": req.full_url,
            "method": req.get_method(),
            "body": json.loads(req.data.decode()) if req.data else None,
            "ctype": req.get_header("Content-type"),
        })
        self.seen.set()
        if self.fail:
            raise OSError("network down")
        rec = self

        class _Resp:
            status = rec.status

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        return _Resp()


@pytest.fixture
def rec(monkeypatch):
    r = _Recorder()
    monkeypatch.setattr(cg.urllib.request, "urlopen", r)
    return r


@pytest.fixture(autouse=True)
def _fast_heartbeat(monkeypatch):
    """Let the tests observe several beats without waiting seconds each.

    The production floor stops a misconfigured interval spinning; it is not
    part of what these tests are asserting.
    """
    monkeypatch.setattr(cg, "_CLAIM_MIN_INTERVAL_S", 0.01)


def _hb(**kw):
    kw.setdefault("url", "http://red5:8675/input-claim")
    kw.setdefault("owner", "cece")
    kw.setdefault("ttl_s", 45.0)
    kw.setdefault("interval_s", 15.0)
    return cg.ClaimHeartbeat(**kw)


def test_posts_immediately_on_start(rec):
    """The first claim is the one that matters — the collision it prevents
    happens at the start of the conversation, not 15s in."""
    hb = _hb()
    hb.start()
    assert rec.seen.wait(2.0), "no POST within 2s of start()"
    hb.stop()
    assert rec.calls[0]["method"] == "POST"
    assert rec.calls[0]["url"] == "http://red5:8675/input-claim"


def test_the_body_is_what_red5_expects(rec):
    hb = _hb()
    hb.start()
    rec.seen.wait(2.0)
    hb.stop()
    body = rec.calls[0]["body"]
    assert body["owner"] == "cece"
    assert body["ttl_s"] == 45.0
    assert body["source"] == "phone-mic"
    assert rec.calls[0]["ctype"] == "application/json"


def test_it_re_asserts_rather_than_claiming_once(rec):
    """Stopping the re-assert is the release, so the re-assert must happen."""
    hb = _hb(ttl_s=1.0, interval_s=0.05)
    hb.start()
    time.sleep(0.4)
    hb.stop()
    assert len(rec.calls) >= 3, f"only {len(rec.calls)} posts — not a heartbeat"


def test_stop_ends_the_posting(rec):
    hb = _hb(ttl_s=1.0, interval_s=0.05)
    hb.start()
    time.sleep(0.2)
    hb.stop()
    after = len(rec.calls)
    time.sleep(0.3)
    assert len(rec.calls) == after, "still posting after stop()"


def _wait_for(predicate, timeout=2.0):
    """The release is deliberately off-thread; give it a moment to land."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_stop_deletes_the_claim(rec):
    """Falling silent releases the claim but NOT the speech hold it set.

    The hold rides 1.5x past the claim's TTL and only a DELETE lifts it early,
    so without this a four-second mic blip silenced Sam for the next 67
    seconds — which is what "TTS is broken" looked like from the outside.
    """
    hb = _hb()
    hb.start()
    assert rec.seen.wait(2.0)
    hb.stop()
    assert _wait_for(lambda: any(c["method"] == "DELETE" for c in rec.calls)), \
        "stop() sent no DELETE — the speech hold rides its full TTL"
    delete = [c for c in rec.calls if c["method"] == "DELETE"][0]
    assert delete["url"] == "http://red5:8675/input-claim?owner=cece", \
        "the DELETE must name its owner, or it lifts nobody's hold"


def test_a_release_that_fails_is_absorbed(monkeypatch):
    """Exactly the old behaviour when it fails: the hold expires on its own.

    A DELETE that can fail is only safe because failing costs nothing, so the
    failure must not escape the thread it runs on or wedge the guard.
    """
    r = _Recorder(fail=True)
    monkeypatch.setattr(cg.urllib.request, "urlopen", r)
    hb = _hb()
    hb.start()
    assert r.seen.wait(2.0)
    hb.stop()                      # must not raise
    assert _wait_for(lambda: any(c["method"] == "DELETE" for c in r.calls))
    hb.start()                     # and the heartbeat still works afterwards
    hb.stop()


def test_a_release_in_flight_cannot_strand_a_new_claim(monkeypatch):
    """Blip, then a real conversation: the second claim must survive.

    stop() releases off-thread, so a mic that goes hot again immediately can
    have its fresh claim DELETEd by the previous cycle's release. The claim is
    per-owner and both cycles are cece, so the server cannot tell them apart —
    the check belongs here.
    """
    r = _Recorder()
    gate = threading.Event()
    original = r.__call__

    def slow(req, timeout=None):
        if req.get_method() == "DELETE":
            gate.wait(2.0)
        return original(req, timeout)

    monkeypatch.setattr(cg.urllib.request, "urlopen", slow)
    hb = _hb(interval_s=30.0)      # no beat will come along and paper over it
    hb.start()
    assert r.seen.wait(2.0)
    hb.stop()                      # the release blocks in the gate
    hb.start()                     # the mic is hot again, and claims
    gate.set()                     # now the stale release lands
    assert _wait_for(lambda: any(c["method"] == "DELETE" for c in r.calls))
    assert _wait_for(lambda: r.calls[-1]["method"] == "POST"), \
        "a stale release cleared the live claim and left it cleared"


def test_an_interval_at_or_above_the_ttl_is_clamped():
    """Misconfiguration must degrade to 'claims more often', never to
    'silently claims nothing' — an interval past the TTL holds nothing."""
    hb = _hb(ttl_s=45.0, interval_s=90.0)
    assert hb._interval_s <= 45.0 / 2


def test_no_url_means_the_feature_is_simply_off(rec):
    hb = _hb(url="")
    hb.start()
    time.sleep(0.15)
    assert not hb.active
    assert rec.calls == []


def test_network_failure_never_escapes(monkeypatch):
    """The duck is a working feature; the claim rides on it and must not
    be able to take it down."""
    r = _Recorder(fail=True)
    monkeypatch.setattr(cg.urllib.request, "urlopen", r)
    hb = _hb(ttl_s=1.0, interval_s=0.05)
    hb.start()
    assert r.seen.wait(2.0)
    time.sleep(0.2)
    assert hb.active, "a failing POST killed the heartbeat thread"
    hb.stop()          # must not raise
    assert len(r.calls) >= 2, "gave up after the first failure"


def test_start_is_idempotent(rec):
    hb = _hb(ttl_s=1.0, interval_s=0.05)
    hb.start()
    t1 = hb._thread
    hb.start()
    assert hb._thread is t1, "second start() spawned a second thread"
    hb.stop()


def test_stop_without_start_is_harmless():
    _hb().stop()


def test_the_thread_is_a_daemon(rec):
    """It must never hold the guard open on shutdown."""
    hb = _hb()
    hb.start()
    assert hb._thread is not None and hb._thread.daemon
    hb.stop()
