"""Health check for the thing that WRITES the external-hold flag.

call_guard cannot tell a working mic-detect trigger from a dead one — both look
like "no flag". In August 2026 the Automate flow that writes it was killed by
battery optimisation and stayed dead for two days; every service reported
healthy, and the only symptom was that speech barge-in quietly stopped working.
These tests cover turning that silence into a reported problem.
"""

import time

import pytest

from agent_media_core import call_guard
from agent_media_core.cli import _mic_detect_facts, health_problems


@pytest.fixture(autouse=True)
def _state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_MIC_DETECT_QUIET_MAX_S", raising=False)


def _age(path, seconds):
    st = path.stat()
    import os
    os.utime(path, (st.st_atime, st.st_mtime - seconds))


def test_no_guard_here_means_no_facts():
    """Only the phone runs the guard; every other host must stay silent rather
    than report a zero that reads as 'never fired'."""
    assert _mic_detect_facts() == {}


def test_a_freshly_started_guard_is_not_flagged():
    """The guard has had no chance to see a hold yet. Flagging that would teach
    everyone to ignore the warning."""
    call_guard.publish_flag_path(call_guard.Config())
    facts = _mic_detect_facts()
    assert facts["mic_detect"] == "watched"
    assert int(facts["mic_detect_quiet_s"]) < 5
    assert health_problems(facts) == []


def test_a_long_running_guard_that_never_saw_a_hold_is_flagged():
    """This is the dead-trigger case: guard fine, nothing writing the flag."""
    call_guard.publish_flag_path(call_guard.Config())
    _age(call_guard.advert_path(), 60 * 60 * 30)      # up 30h, never a hold
    facts = _mic_detect_facts()
    assert "mic_detect_last_hold_s" not in facts
    problems = health_problems(facts)
    assert any("mic-detect quiet" in p and "never fired" in p for p in problems)


def test_a_recent_hold_clears_the_problem():
    call_guard.publish_flag_path(call_guard.Config())
    _age(call_guard.advert_path(), 60 * 60 * 30)
    call_guard.note_external_hold()                    # fired just now
    facts = _mic_detect_facts()
    assert int(facts["mic_detect_last_hold_s"]) < 5
    assert health_problems(facts) == []


def test_a_stale_hold_is_flagged_with_its_age():
    call_guard.publish_flag_path(call_guard.Config())
    call_guard.note_external_hold()
    _age(call_guard.advert_path(), 60 * 60 * 50)
    _age(call_guard.last_hold_path(), 60 * 60 * 50)    # last fired 50h ago
    problems = health_problems(_mic_detect_facts())
    assert len(problems) == 1
    assert "50h ago" in problems[0]
    assert "barge-in fails silently" in problems[0]


def test_the_threshold_is_tunable(monkeypatch):
    call_guard.publish_flag_path(call_guard.Config())
    call_guard.note_external_hold()
    _age(call_guard.advert_path(), 60 * 90)
    _age(call_guard.last_hold_path(), 60 * 90)         # 90 minutes
    assert health_problems(_mic_detect_facts()) == []  # under the 24h default

    monkeypatch.setenv("MEDIA_MIC_DETECT_QUIET_MAX_S", "3600")
    assert health_problems(_mic_detect_facts())        # over a 1h limit


def test_zero_disables_the_check(monkeypatch):
    """An escape hatch for a host where nothing should ever write the flag."""
    call_guard.publish_flag_path(call_guard.Config())
    _age(call_guard.advert_path(), 60 * 60 * 200)
    monkeypatch.setenv("MEDIA_MIC_DETECT_QUIET_MAX_S", "0")
    assert health_problems(_mic_detect_facts()) == []


def test_a_guard_restart_does_not_reset_the_quiet_clock():
    """The clock used to run from the LATER of guard-start and last hold, so
    every restart of a supervised service reset it. Deploy, crash or reboot
    more often than the limit and a permanently dead trigger is never reported
    — and a restart is exactly when nobody is watching for it."""
    call_guard.publish_flag_path(call_guard.Config())
    call_guard.note_external_hold()
    _age(call_guard.last_hold_path(), 60 * 60 * 50)    # last fired 50h ago
    # ...and the guard came up a minute ago, as it does after any deploy.
    _age(call_guard.advert_path(), 60)

    facts = _mic_detect_facts()
    assert int(facts["mic_detect_quiet_s"]) > 60 * 60 * 49
    assert any("mic-detect quiet" in p for p in health_problems(facts))


def test_the_heartbeat_records_who_held(monkeypatch, tmp_path):
    """A hold someone typed is not evidence that mic-detect is alive, and both
    write the same flag. Without the source the log of 25 holds yesterday
    cannot answer whether the bridge is running."""
    flag = tmp_path / "hold"
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_FLAG", str(flag))
    cfg = call_guard.Config()
    call_guard.publish_flag_path(cfg)

    call_guard._set_hold(cfg, source="cli")
    call_guard.note_external_hold(call_guard._flag_source(cfg.hold_flag))
    assert _mic_detect_facts()["mic_detect_last_hold_src"] == "cli"


def test_an_unlabelled_flag_is_recorded_as_external(monkeypatch, tmp_path):
    """The Automate bridge writes an empty flag and must keep working. Call it
    what it is — un-attributed — rather than assuming the only writer we know."""
    flag = tmp_path / "hold"
    flag.write_text("")                                # what the bridge writes
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_FLAG", str(flag))
    cfg = call_guard.Config()
    call_guard.publish_flag_path(cfg)

    call_guard.note_external_hold(call_guard._flag_source(cfg.hold_flag))
    assert _mic_detect_facts()["mic_detect_last_hold_src"] == "external"


def test_a_ttl_flag_still_parses_with_a_source(monkeypatch, tmp_path):
    """ttl and src share the flag's body; adding one must not break the other,
    since a TTL that stops expiring leaves music quiet indefinitely."""
    flag = tmp_path / "hold"
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_FLAG", str(flag))
    cfg = call_guard.Config()

    call_guard._set_hold(cfg, ttl=120, source="cece")
    assert call_guard._flag_ttl(cfg.hold_flag) == 120
    assert call_guard._flag_source(cfg.hold_flag) == "cece"


def test_only_the_flag_path_counts_as_proof_of_life(monkeypatch, tmp_path):
    """A phone CALL also ducks audio, but says nothing about whether mic-detect
    is alive — so it must not tick the heartbeat."""
    flag = tmp_path / "hold"
    monkeypatch.setenv("MEDIA_CALL_GUARD_HOLD_FLAG", str(flag))
    cfg = call_guard.Config()
    call_guard.publish_flag_path(cfg)
    assert not call_guard.last_hold_path().exists()

    # What the run loop does on an external hold, and only then.
    call_guard.note_external_hold()
    assert call_guard.last_hold_path().exists()
