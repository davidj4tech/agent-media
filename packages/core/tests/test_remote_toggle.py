"""Pause must not depend on an observation that can fail.

The remote toggle read the player to learn whether anything was playing, then
wrote back the opposite of what it found. Over a bridge that loses a fifth of
its packets that read fails regularly with nothing wrong at the far end — and
a failed read is indistinguishable from "nothing is playing", so Space fell
through to the replay branch instead of pausing. On this lane replay loads a
pseudo-URI mpv cannot open, so the keypress did nothing whatsoever, sometimes,
for no reason the user could see.

Whether a reply is in flight now comes from now_playing (written on this host,
cannot be lost in transit) and the toggle is a single atomic write.

That write says `pause=<value>` rather than mpv's `cycle pause`: the phone lane
ends at the companion app as often as at mpv, the app answers a subset of the
verbs and refused `cycle` — and fire-and-forget never hears a refusal, so the
key went dead again, on the lane it is pressed on most. The value to write is
already on the row, because the follow-along clock needs it written down.
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
    assert remote == [("set_property", "pause", True)], (
        "a keypress consulted the network to decide what to do, and a lost "
        "packet turned pause into an impossible replay")


def test_toggle_is_atomic_not_read_then_write(remote, monkeypatch):
    """One write, not get+set: two round trips race the renderer, which clears
    pause before each clip, and cost twice the latency to do one thing."""
    _speaking(monkeypatch)
    monkeypatch.setattr(cli.ipc, "get_properties",
                        lambda *a, **k: {"idle-active": False, "pause": True})

    cli.cmd_toggle(None)
    assert remote == [("set_property", "pause", True)]


def test_toggle_is_a_value_the_app_understands(remote, monkeypatch):
    """`cycle` is mpv's; the companion app answering the same socket rejects
    it, and a fire-and-forget command never hears the rejection."""
    _speaking(monkeypatch)
    cli.cmd_toggle(None)
    assert remote and remote[0][0] == "set_property", (
        "the app refuses verbs it does not implement, so a toggle spelled as "
        "`cycle` is a keypress that silently does nothing")


def test_the_flip_comes_from_the_row(remote, monkeypatch):
    """Which way to flip is read from the pause already written down, not from
    the player — that is the whole point of not asking the network."""
    import os
    monkeypatch.setattr(cli, "_now_speaking",
                        lambda: {"extras": {"writer_pid": os.getpid(),
                                            "paused_at": 1.0}})
    cli.cmd_toggle(None)
    assert remote == [("set_property", "pause", False)], (
        "a second press on a paused reply has to resume it")


def test_idle_still_replays(remote, monkeypatch):
    monkeypatch.setattr(cli, "_now_speaking", lambda: None)
    cli.cmd_toggle(None)
    assert remote == [("REPLAY",)]


def test_dead_writer_is_not_in_flight(remote, monkeypatch):
    """A crashed submit process must not leave pause addressing a ghost."""
    _speaking(monkeypatch, alive=False)
    cli.cmd_toggle(None)
    assert remote == [("REPLAY",)]
