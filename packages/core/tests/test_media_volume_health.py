"""A phone whose media volume is zero is silent, and looks perfectly well.

mpv plays into Android's STREAM_MUSIC. With that at zero the player is
unpaused, unmuted and at volume 150, the renderer answers, every service is up
— and nothing is audible. The same shape of failure as a dead mic-detect
trigger, and it took the same hour to find.

Reported, never corrected: someone silencing their phone means it.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from agent_media_core import cli


def _volumes(monkeypatch, streams, rc=0, which="/usr/bin/termux-volume"):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: which)

    class _R:
        returncode = rc
        stdout = json.dumps(streams)

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())


def test_silence_is_reported_as_a_problem(monkeypatch):
    _volumes(monkeypatch, [{"stream": "music", "volume": 0, "max_volume": 25}])
    facts = cli._media_volume_facts()
    assert facts == {"media_volume": "0/25"}
    assert any("media volume is 0" in p for p in cli.health_problems(facts))


def test_an_audible_phone_is_reported_but_not_a_problem(monkeypatch):
    """The level is worth knowing — 1/25 explains 'I can barely hear it' — but
    only zero is a fault."""
    _volumes(monkeypatch, [{"stream": "music", "volume": 12, "max_volume": 25}])
    facts = cli._media_volume_facts()
    assert facts == {"media_volume": "12/25"}
    assert cli.health_problems(facts) == []


def test_hosts_without_termux_say_nothing(monkeypatch):
    """red5 and pn have no Android streams; a fact of 0 there would be a lie."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert cli._media_volume_facts() == {}


def test_a_wedged_or_missing_termux_api_is_not_a_problem(monkeypatch):
    """termux-volume needs the Termux:API app; without it the command hangs or
    fails. A health check must not invent a verdict from that."""
    _volumes(monkeypatch, [], rc=1)
    assert cli._media_volume_facts() == {}

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired("termux-volume", 15)

    monkeypatch.setattr(subprocess, "run", _boom)
    assert cli._media_volume_facts() == {}


def test_garbage_output_is_not_a_problem(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/termux-volume")

    class _R:
        returncode = 0
        stdout = "not json"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    assert cli._media_volume_facts() == {}


def test_no_music_stream_in_the_list(monkeypatch):
    _volumes(monkeypatch, [{"stream": "call", "volume": 15, "max_volume": 15}])
    assert cli._media_volume_facts() == {}
