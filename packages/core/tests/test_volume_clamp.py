"""Tests for `media volume` clamping.

The popup's -/= keys nudge the broker's *software* volume, which runs above
mpv's nominal 100 (the broker launches with --volume-max=200). The ceiling has
to be that max: a lower one made the first press of either key snap the volume
down to the clamp, so "louder" made speech quieter.
"""

import argparse

import pytest

from agent_media_core import cli


@pytest.fixture
def broker(monkeypatch):
    """A fake broker: records what volume it's set to, reports what it holds."""
    class Broker:
        volume = 150

        def get(self, prop):
            return Broker.volume if prop == "volume" else None

        def set(self, sock, prop, value):
            assert prop == "volume"
            Broker.volume = value

    monkeypatch.setattr(cli, "_sock", lambda: "sock")
    monkeypatch.setattr(cli, "_get", Broker().get)
    monkeypatch.setattr(cli.ipc, "set_property", Broker().set)
    monkeypatch.setattr(cli, "_broker_max_volume", lambda: 200.0)
    return Broker


def _vol(delta):
    cli.cmd_volume(argparse.Namespace(delta=delta))


def test_up_from_the_resting_level_goes_up(broker):
    _vol(5)
    assert broker.volume == 155


def test_clamps_to_the_broker_max_not_a_lower_number(broker):
    broker.volume = 198
    _vol(5)
    assert broker.volume == 200
    _vol(5)
    assert broker.volume == 200


def test_never_goes_below_zero(broker):
    broker.volume = 3
    _vol(-5)
    assert broker.volume == 0


def test_follows_a_reconfigured_ceiling(broker, monkeypatch):
    """A host that lowers MEDIA_SPEECH_VOLUME_MAX lowers the key's ceiling too —
    mpv refuses a volume above its own max, so they can't drift apart."""
    monkeypatch.setattr(cli, "_broker_max_volume", lambda: 120.0)
    broker.volume = 118
    _vol(5)
    assert broker.volume == 120
