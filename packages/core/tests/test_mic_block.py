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


def test_what_it_saw_is_published_for_doctor(tmp_path, monkeypatch):
    """The service is the only thing on the phone that can read the app-op, so
    it writes down what it saw. Without this, `media doctor` has to infer the
    block's state from the hold rate — which cannot tell a reverted block from
    a person using their microphone."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    import json as _json

    mic_block.publish("com.google.android.as", "ignore", None)
    blob = _json.loads(mic_block.state_path().read_text())
    assert blob["com.google.android.as"]["mode"] == "ignore"
    assert blob["com.google.android.as"]["checked_at"] > 0
    assert "reverts" not in blob["com.google.android.as"]

    mic_block.publish("com.google.android.as", "ignore", 1000.0)
    mic_block.publish("com.google.android.as", "ignore", 2000.0)
    blob = _json.loads(mic_block.state_path().read_text())
    assert blob["com.google.android.as"]["reverts"] == [1000.0, 2000.0]
    assert blob["com.google.android.as"]["last_revert_at"] == 2000.0


def test_a_days_worth_of_reverts_is_kept_not_a_lifetime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    import json as _json

    mic_block.publish("p", "ignore", 1000.0)
    mic_block.publish("p", "ignore", 1000.0 + 86400 * 2)
    blob = _json.loads(mic_block.state_path().read_text())
    assert blob["p"]["reverts"] == [1000.0 + 86400 * 2], "yesterday's still counted"


def test_unreadable_state_is_no_state(tmp_path, monkeypatch):
    """Corrupt or missing means the service does not run here — never a claim
    that the block is off, which would be a fault report on every host."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from agent_media_core import cli

    assert cli._mic_block_facts() == {}
    mic_block.state_path().parent.mkdir(parents=True, exist_ok=True)
    mic_block.state_path().write_text("{ not json")
    assert cli._mic_block_facts() == {}


def test_losing_the_shell_is_not_the_same_as_losing_the_block(
        tmp_path, monkeypatch):
    """`unknown` means "we cannot see", and `tick` is explicit that this is not
    evidence either way. Flattening it in with a block we watched come off is
    how "no adb here" was reported as "the block is not in force" — asserting
    a consequence (speech paused every half minute) that nothing had observed.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from agent_media_core.cli import _mic_block_facts, health_problems

    mic_block.publish("com.google.android.as", "ignore", None)
    assert _mic_block_facts()["mic_block"] == "held"

    mic_block.publish("com.google.android.as", None, None)
    facts = _mic_block_facts()
    assert facts["mic_block"].startswith("unknown:com.google.android.as")
    assert "last seen ignore" in facts["mic_block"], (
        "a reader has to be able to tell how stale the last real reading is")
    assert health_problems(facts) == [], (
        "a health flag raised on 'cannot see' stands for as long as the phone "
        "is away from a trusted wifi, and it costs the whole status line")


def test_a_block_we_watched_come_off_is_still_a_fault(tmp_path, monkeypatch):
    """The other half: when the shell IS there and the answer is `allow`, that
    is the reverted block, and it must still be reported."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from agent_media_core.cli import _mic_block_facts, health_problems

    mic_block.publish("com.google.android.as", "allow", None)
    facts = _mic_block_facts()
    assert facts["mic_block"] == "loose:com.google.android.as=allow"
    assert any("not in force" in p for p in health_problems(facts))


def test_the_rate_still_speaks_while_we_are_blind(tmp_path, monkeypatch):
    """The symptom of a reverted block IS visible without a shell. Being unable
    to read the op must not also silence the evidence that would show it."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from agent_media_core.cli import _mic_block_facts, health_problems

    mic_block.publish("com.google.android.as", None, None)
    facts = _mic_block_facts()
    facts["dictation_rate"] = "dictation held 96 times in the last hour"
    assert any("96 times" in p for p in health_problems(facts))
