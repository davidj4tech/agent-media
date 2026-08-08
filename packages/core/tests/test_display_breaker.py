"""The display read must not breaker itself off the endpoint it displays.

The slow-endpoint breaker exists so policy chatter ("is anything playing?
duck it") to a distant phone can't delay speech; its latency budget is set
accordingly (~0.7s on red5). But the popup's snapshot of the speech player
crosses the same slow link, where a 2s round trip is the honest cost of an
answer, not a fault. Judged by that budget the breaker sat open essentially
all the time, so a short utterance's few seconds of playback were never once
sampled and the popup read blank while audio was plainly coming out of the
phone. Latency must not trip the breaker for a read whose only cost is its
own staleness — failure still must, or a phone that is simply gone makes
every redraw wait out the timeout.
"""

import time

import pytest

from agent_media_core import _breaker
from agent_media_core.sinks import _mpv_ipc as ipc


EP = "tcp://phone.example:6602"


@pytest.fixture(autouse=True)
def _isolated_breaker(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(ipc, "_breaker_until", None)
    monkeypatch.setenv("MEDIA_MPV_SLOW_MS", "700")
    monkeypatch.setenv("MEDIA_MPV_BREAKER_S", "20")
    yield


def _open_endpoints():
    return set(_breaker.load("mpv"))


def test_slow_success_trips_breaker_by_default():
    ipc._record(EP, elapsed=2.0, failed=False)
    assert EP in _open_endpoints()


def test_slow_success_does_not_trip_a_display_read():
    ipc._record(EP, elapsed=2.0, failed=False, slow_s=0)
    assert _open_endpoints() == set(), (
        "ordinary bridge latency breakered the display path — short "
        "utterances will go unobserved and the popup will read blank")


def test_failure_still_trips_a_display_read():
    ipc._record(EP, elapsed=0.01, failed=True, slow_s=0)
    assert EP in _open_endpoints(), (
        "an unreachable bridge must still be skipped, or every redraw "
        "pays the connect timeout")


def test_display_read_closes_a_breaker_opened_by_chatter():
    ipc._record(EP, elapsed=2.0, failed=False)          # chatter trips it
    assert EP in _open_endpoints()
    ipc._record(EP, elapsed=2.0, failed=False, slow_s=0)  # display answers fine
    assert _open_endpoints() == set()


def test_slow_control_keypress_does_not_blank_the_display(monkeypatch):
    """A control that bypasses the skip must not set a deadline for others.

    Transport controls are `critical`, so they attempt however slow the link
    is — but they were still recording that slowness, which opened the breaker
    that the *display* read honours. One `pause` at 5s on a 450ms-RTT link
    blanked the popup for the whole cool-off window, so the keypress worked and
    the screen sat unchanged. That is indistinguishable from a dead control,
    and it is what "the controls aren't responding" turned out to mean.
    """
    def fake_send_inner(sock, command, timeout):
        time.sleep(0.02)
        return {"error": "success", "data": None}

    monkeypatch.setattr(ipc, "_send_inner", fake_send_inner)
    monkeypatch.setenv("MEDIA_MPV_SLOW_MS", "1")   # anything counts as slow
    monkeypatch.setattr(ipc, "_breaker_until", None)

    ipc.command(EP, "set_property", "pause", True, critical=True)
    assert _open_endpoints() == set(), (
        "a slow keypress breakered the endpoint the popup reads — the control "
        "lands, the display freezes, and it looks like nothing happened")


def test_failed_control_still_trips_the_breaker(monkeypatch):
    def boom(sock, command, timeout):
        raise OSError("unreachable")

    monkeypatch.setattr(ipc, "_send_inner", boom)
    monkeypatch.setattr(ipc, "_breaker_until", None)
    with pytest.raises((ipc.MpvIpcError, OSError)):
        ipc.command(EP, "set_property", "pause", True, critical=True)
    assert EP in _open_endpoints()


def test_remote_snapshot_asks_for_the_latency_exemption(monkeypatch):
    """The wiring, not just the rule: this kwarg is the whole fix."""
    from agent_media_core import cli

    seen = {}

    def fake_get_properties(sock, names, **kw):
        seen.update(kw)
        return {"idle-active": False, "time-pos": 1.0, "duration": 4.0}

    monkeypatch.setattr(cli.ipc, "get_properties", fake_get_properties)
    monkeypatch.setattr(cli, "_sock", lambda: EP)
    monkeypatch.setenv("MEDIA_REMOTE_SNAPSHOT_TTL", "0")   # no cache hit
    cli._SNAP_CACHE["value"] = None

    assert cli._remote_snapshot() is not None
    assert seen.get("slow_s") == 0
