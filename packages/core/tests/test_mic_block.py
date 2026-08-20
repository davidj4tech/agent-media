"""Holding a mic block that the platform keeps undoing.

Every string parsed here is real `appops get` output from p8a, including the
one that mattered most: a uid mode saying `ignore` above a package line still
saying `allow`. Reading the wrong line is how a re-apply looked successful
while the recogniser carried on holding the microphone.
"""

from unittest import mock

import pytest

from agent_media_core import mic_block


BLOCKED_OUT = """Uid mode: RECORD_AUDIO: ignore
  RECORD_AUDIO: allow; time=+9m43s53ms ago; rejectTime=+2h5m42s780ms ago; duration=+22s135ms
"""

REVERTED_OUT = """Uid mode: RECORD_AUDIO: foreground
  RECORD_AUDIO: allow; time=+9m14s836ms ago; rejectTime=+2h5m14s563ms ago; duration=+22s135ms
"""


def test_the_uid_line_is_the_one_that_decides():
    """The package line says `allow` in both. Only the uid mode differs, and
    only the uid mode is in force."""
    assert mic_block.parse_uid_mode(BLOCKED_OUT) == "ignore"
    assert mic_block.parse_uid_mode(REVERTED_OUT) == "foreground"


def test_output_it_cannot_read_is_not_permission_to_write():
    """None means "could not tell", never "not blocked" — otherwise every
    failed read turns into a write against a phone we cannot see."""
    assert mic_block.parse_uid_mode("") is None
    assert mic_block.parse_uid_mode("error: device offline") is None
    assert mic_block.parse_uid_mode("RECORD_AUDIO: allow; time=+1s ago") is None


def test_a_block_in_force_is_left_alone():
    with mock.patch.object(mic_block, "read_mode", return_value="ignore"), \
         mock.patch.object(mic_block, "apply_block") as applied:
        outcome, last = mic_block.tick("com.google.android.as", 1000.0)
    assert outcome == "held"
    assert last == 1000.0, "a quiet tick must not restamp the clock"
    applied.assert_not_called()


def test_a_reverted_block_is_re_applied_and_dated():
    with mock.patch.object(mic_block, "read_mode", return_value="foreground"), \
         mock.patch.object(mic_block, "apply_block", return_value=True):
        outcome, last = mic_block.tick("com.google.android.as", 1000.0,
                                       now=8200.0)
    assert outcome == "reverted"
    assert last == 8200.0, "the re-apply is what the next interval is measured from"


def test_an_unreachable_phone_changes_nothing():
    """adb dropping its pairing says nothing about the block, so the tick must
    not report it as either state — and must not restamp the clock, or the
    next real revert reads as having stood for no time at all."""
    with mock.patch.object(mic_block, "read_mode", return_value=None), \
         mock.patch.object(mic_block, "apply_block") as applied:
        outcome, last = mic_block.tick("com.google.android.as", 1000.0)
    assert outcome == "unreadable"
    assert last == 1000.0
    applied.assert_not_called()


def test_a_re_apply_that_does_not_take_is_not_success():
    """apply_block reads the mode back rather than trusting adb's exit code:
    `appops set` without --uid exits 0 and changes nothing in force."""
    with mock.patch.object(mic_block, "read_mode", return_value="foreground"), \
         mock.patch.object(mic_block, "apply_block", return_value=False):
        outcome, last = mic_block.tick("com.google.android.as", 1000.0)
    assert outcome == "failed"
    assert last == 1000.0


def test_apply_writes_the_uid_op_and_verifies_it():
    calls = []

    def fake_adb(args, timeout=20.0):
        calls.append(args)
        if args[1] == "set":
            return 0, ""
        return 0, BLOCKED_OUT

    with mock.patch.object(mic_block, "_adb", side_effect=fake_adb):
        assert mic_block.apply_block("com.google.android.as") is True
    assert calls[0] == ["shell", "appops", "set", "--uid",
                        "com.google.android.as", "RECORD_AUDIO", "ignore"], \
        "without --uid the write is overridden by the uid mode"
    assert calls[1][:3] == ["shell", "appops", "get"], "the write is verified"


@pytest.mark.parametrize("raw,expected", [
    ("", []),
    ("com.google.android.as", ["com.google.android.as"]),
    ("a.b, c.d", ["a.b", "c.d"]),
    ("  ", []),
])
def test_nothing_is_blocked_unless_someone_asked(monkeypatch, raw, expected):
    """Silencing an app's microphone is a decision. An unset variable is the
    normal state of every host that is not David's phone."""
    monkeypatch.setenv("MEDIA_MIC_BLOCK_PACKAGES", raw)
    assert mic_block.packages() == expected


def test_the_interval_has_a_floor(monkeypatch):
    monkeypatch.setenv("MEDIA_MIC_BLOCK_INTERVAL_S", "1")
    assert mic_block.interval_s() >= 30.0
    monkeypatch.setenv("MEDIA_MIC_BLOCK_INTERVAL_S", "not a number")
    assert mic_block.interval_s() == mic_block.DEFAULT_INTERVAL_S
