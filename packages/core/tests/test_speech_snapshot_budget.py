"""The follow-along read must not be judged by the policy-chatter budget.

`snapshot` is what the coordinator uses to watch a reply play. On p8a the
in-app player answers it in a steady 1.28s — 0.43s of that is the tailnet
connect — against a default slow line of 1200ms. Eighty milliseconds over, on
every tick, and the breaker then skipped the endpoint for twenty seconds; the
follow loop went blind, called the playlist stalled, and held the global speech
token for the reply's whole duration. That is how replies came to be spoken ten
minutes late on a phone that was idle most of the hour.
"""

import time
from unittest import mock

import pytest

from agent_media_core.sinks import _mpv_ipc as ipc
from agent_media_core.sinks.speech import SinkSpeech
from agent_media_core.types import Target


@pytest.fixture(autouse=True)
def _remote_app(monkeypatch):
    """A tcp:// endpoint of our own, and a breaker that starts closed.

    Named explicitly rather than inherited from whatever this host's
    agent-media.env says, because the breaker only applies to *remote*
    endpoints — a machine whose app target happened to be a unix socket would
    pass every assertion here without exercising anything.
    """
    monkeypatch.setenv("MEDIA_SPEECH_SOCKET_APP", "tcp://p8a.test:6612")
    ipc.reset_breaker()
    yield
    ipc.reset_breaker()


def test_snapshot_is_not_judged_by_the_chatter_budget():
    with mock.patch.object(ipc, "get_properties", return_value={}) as got:
        SinkSpeech().snapshot(Target("app"))
    kwargs = got.call_args.kwargs
    assert kwargs["slow_s"] == 0, "a slow honest answer must not trip the breaker"
    assert kwargs["breaker_s"] == 5, "a dead phone should still be skipped, briefly"
    assert kwargs["timeout"] >= 3.0, "1.28s answers need room, or idle-active is lost"


def test_an_honest_slow_endpoint_is_still_readable_next_tick(monkeypatch):
    """The regression itself, at the transport layer: answer at p8a's real
    latency twice in a row and the second read must still happen."""
    calls = []

    def slow_once(sock_path, names, timeout):
        calls.append(time.monotonic())
        time.sleep(1.3)                      # p8a's measured answer
        return {n: 0 for n in names}, len(names)

    monkeypatch.setattr(ipc, "_get_properties_once", slow_once)
    sink = SinkSpeech()
    first = sink.snapshot(Target("app"))
    second = sink.snapshot(Target("app"))
    assert first and second, "the second tick was skipped as 'endpoint slow'"
    assert len(calls) == 2


def test_a_dead_endpoint_still_opens_the_breaker(monkeypatch):
    """Latency is forgiven; failure is not. Otherwise every tick against a
    phone that is genuinely gone waits out the connect timeout."""

    def dead(sock_path, names, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(ipc, "_get_properties_once", dead)
    sink = SinkSpeech()
    assert sink.snapshot(Target("app")) == {}
    with mock.patch.object(ipc, "_get_properties_once", side_effect=dead) as again:
        assert sink.snapshot(Target("app")) == {}
        assert again.call_count == 0, "a failed endpoint should be skipped, not retried"
