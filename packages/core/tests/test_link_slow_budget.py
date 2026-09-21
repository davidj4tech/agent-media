"""A phone that is far away is not a phone that is not answering.

The slow-endpoint breaker judged every policy call against a flat 1.2s. On
21 Sep p8a was at 430ms RTT off home Wi-Fi, where an honest five-property read
takes 1.30s, so the music endpoint's breaker was open whenever a reply asked
it anything — and the probe that decides whether to duck music under speech
was skipped, so speech played over music at full volume.

The budget is now a number of round trips on the link, measured from the
connects the calls already make.
"""

import pytest

from agent_media_core import _breaker
from agent_media_core.sinks import _mpv_ipc as ipc


EP = "tcp://phone.example:6601"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(ipc, "_breaker_until", None)
    monkeypatch.setattr(ipc, "_connect_s", {})
    monkeypatch.setenv("MEDIA_MPV_SLOW_MS", "1200")
    monkeypatch.setenv("MEDIA_MPV_BREAKER_S", "20")
    monkeypatch.delenv("MEDIA_MPV_SLOW_CAP_MS", raising=False)


def _open():
    return set(_breaker.load("mpv"))


def _link(rtt, n=3):
    ipc._connect_s[EP] = [rtt] * n


def test_an_honest_read_over_a_distant_link_does_not_trip():
    _link(0.43)                                   # p8a, 21 Sep
    ipc._record(EP, elapsed=1.30, failed=False)   # five properties, measured
    assert _open() == set(), (
        "a healthy read on a 430ms link breakered the music endpoint — the "
        "duck probe is then skipped and speech plays over the music")


def test_a_call_far_slower_than_its_link_still_trips():
    _link(0.43)
    ipc._record(EP, elapsed=2.5, failed=False)
    assert EP in _open()


def test_a_lossy_link_cannot_raise_the_budget_past_the_cap():
    """Every connect retransmitting inflates the estimate itself; the cap is
    what keeps the breaker's original job — a dozen 2s+ calls delaying
    speech by ~24s — from quietly switching off."""
    _link(1.4)                                    # every SYN lost once
    ipc._record(EP, elapsed=3.5, failed=False)
    assert EP in _open()


def test_one_lost_syn_does_not_inflate_the_estimate():
    ipc._connect_s[EP] = [0.43, 1.43, 0.44]       # the middle one retransmitted
    assert ipc._link_slow_s(EP) == pytest.approx(4 * 0.43)


def test_an_unmeasured_endpoint_keeps_the_default_budget():
    assert ipc._link_slow_s(EP) == pytest.approx(1.2)
    ipc._record(EP, elapsed=1.30, failed=False)
    assert EP in _open()


def test_a_near_link_never_lowers_the_default():
    _link(0.005)                                  # same room
    assert ipc._link_slow_s(EP) == pytest.approx(1.2)


def test_an_explicit_budget_is_left_alone():
    _link(0.43)
    ipc._record(EP, elapsed=1.30, failed=False, slow_s=0.5)
    assert EP in _open()


def test_failure_trips_whatever_the_link():
    _link(0.43)
    ipc._record(EP, elapsed=0.01, failed=True)
    assert EP in _open()


def test_connects_are_measured_and_bounded(monkeypatch):
    """_open records its own connect time, and keeps only the recent ones."""
    class _Sock:
        def settimeout(self, t):
            pass

        def connect(self, addr):
            pass

    monkeypatch.setattr(ipc.socket, "socket", lambda *a, **k: _Sock())
    for _ in range(ipc._CONNECT_SAMPLES + 5):
        ipc._open(EP, timeout=1)
    assert len(ipc._connect_s[EP]) == ipc._CONNECT_SAMPLES
