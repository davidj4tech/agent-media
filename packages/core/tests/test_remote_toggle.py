"""Pause must not depend on an observation that can fail.

The remote toggle read the player to learn whether anything was playing, then
wrote back the opposite of what it found. Over a bridge that loses a fifth of
its packets that read fails regularly with nothing wrong at the far end — and
a failed read is indistinguishable from "nothing is playing", so Space fell
through to the replay branch instead of pausing. On this lane replay loads a
pseudo-URI mpv cannot open, so the keypress did nothing whatsoever, sometimes,
for no reason the user could see.

Whether a reply is in flight now comes from now_playing (written on this host,
cannot be lost in transit) and the toggle is a single atomic `cycle pause`.
"""

import pytest

from agent_media_core import cli


@pytest.fixture
def remote(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_remote_speech", lambda: True)
    monkeypatch.setattr(cli, "_sock", lambda: "tcp://phone.example:6602")
    sent = []
    monkeypatch.setattr(cli.ipc, "send_nowait",
                        lambda sock, *a, **k: sent.append(a))
    monkeypatch.setattr(cli, "_do_replay",
                        lambda *a, **k: sent.append(("REPLAY",)) or 0)
    return sent


def _speaking(monkeypatch, alive=True):
    import os
    pid = os.getpid() if alive else 999999
    monkeypatch.setattr(cli, "_now_speaking",
                        lambda: {"extras": {"writer_pid": pid}})


def test_pause_works_when_the_player_cannot_be_read(remote, monkeypatch):
    """The regression: a dropped read used to turn pause into a no-op."""
    _speaking(monkeypatch)

    def unreachable(*a, **k):
        raise cli.ipc.MpvIpcError("packet lost")

    monkeypatch.setattr(cli.ipc, "get_properties", unreachable)

    assert cli.cmd_toggle(None) == 0
    assert remote == [("cycle", "pause")], (
        "a keypress consulted the network to decide what to do, and a lost "
        "packet turned pause into an impossible replay")


def test_toggle_is_atomic_not_read_then_write(remote, monkeypatch):
    """`cycle`, not get+set: two round trips race the renderer, which clears
    pause before each clip, and cost twice the latency to do one thing."""
    _speaking(monkeypatch)
    monkeypatch.setattr(cli.ipc, "get_properties",
                        lambda *a, **k: {"idle-active": False, "pause": True})

    cli.cmd_toggle(None)
    assert remote == [("cycle", "pause")]


def test_idle_still_replays(remote, monkeypatch):
    monkeypatch.setattr(cli, "_now_speaking", lambda: None)
    cli.cmd_toggle(None)
    assert remote == [("REPLAY",)]


def test_dead_writer_is_not_in_flight(remote, monkeypatch):
    """A crashed submit process must not leave pause addressing a ghost."""
    _speaking(monkeypatch, alive=False)
    cli.cmd_toggle(None)
    assert remote == [("REPLAY",)]
