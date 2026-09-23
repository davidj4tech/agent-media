"""A speed step ladders off the last press, not off the phone.

`media speed up` is read-compute-write: read the rate, pick the next rung,
write it. The read used to be a snapshot of the player — a ~2s round trip over
a link that loses a fifth of its packets — so two presses made inside one round
trip both read the same rung and both landed on it. The app's speech bar
computes the ladder optimistically, so its label ran ahead of what was playing:
"I set it to 2x on the clip before... it was 1.5x" (David, 23 Sep 2026).

The rate the last press set is written down here, so the read is a file read:
no round trip to wait through, and no window for the next press to race.
"""

import pytest

from agent_media_core import cli


class _Args:
    def __init__(self, factor):
        self.factor = factor


@pytest.fixture
def phone(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_remote_speech", lambda: True)
    monkeypatch.setattr(cli, "_sock", lambda: "tcp://phone.example:6602")
    monkeypatch.setattr(cli, "_patch_speech_mirror", lambda **k: None)
    sent = []
    monkeypatch.setattr(cli.ipc, "set_property",
                        lambda sock, name, value, **k: sent.append((name, value)))

    # A rate the last press already wrote down — every press but the very
    # first one has one, and the first is covered on its own below.
    cli.StateStore().set_speech_speed(1.0)

    def no_reading(*a, **k):
        raise AssertionError("a keypress asked the phone what rate it was at")
    monkeypatch.setattr(cli, "_speech_display_state", no_reading)
    return sent


def test_consecutive_presses_climb_the_ladder(phone):
    """The regression: the second press used to repeat the first one's rung."""
    for _ in range(3):
        cli.cmd_speed(_Args("up"))
    assert phone == [("speed", 1.25), ("speed", 1.5), ("speed", 2.0)]


def test_a_press_does_not_read_the_player(phone):
    """The fixture fails the test if it does — that read is both the race and
    the delay a listener hears as "it changes at the end of the sentence"."""
    cli.cmd_speed(_Args("up"))
    assert phone == [("speed", 1.25)]


def test_down_comes_back_the_same_way(phone):
    cli.cmd_speed(_Args("2"))
    cli.cmd_speed(_Args("down"))
    assert phone == [("speed", 2.0), ("speed", 1.5)]


def test_reset_is_remembered_too(phone):
    """Or the press after a reset ladders off a rate nothing is playing at."""
    cli.cmd_speed(_Args("2"))
    cli.cmd_speed(_Args("reset"))
    cli.cmd_speed(_Args("up"))
    assert phone[-1] == ("speed", 1.25)


def test_what_is_sent_is_absolute(phone, monkeypatch):
    """Which is what lets the stored rate be trusted: a player that forgot its
    rate (the app restarted) is told the whole value, not a delta, so the two
    agree again on the very first press."""
    cli.cmd_speed(_Args("1.5"))
    assert phone == [("speed", 1.5)]


def test_the_first_press_seeds_from_the_player(monkeypatch, tmp_path):
    """With nothing written down yet there is nothing else to ladder from."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_remote_speech", lambda: True)
    monkeypatch.setattr(cli, "_sock", lambda: "tcp://phone.example:6602")
    monkeypatch.setattr(cli, "_patch_speech_mirror", lambda **k: None)
    monkeypatch.setattr(cli, "_speech_display_state",
                        lambda *a, **k: (False, 0, 0, False, False, 1.5, True))
    sent = []
    monkeypatch.setattr(cli.ipc, "set_property",
                        lambda sock, name, value, **k: sent.append((name, value)))

    cli.cmd_speed(_Args("up"))
    assert sent == [("speed", 2.0)]
