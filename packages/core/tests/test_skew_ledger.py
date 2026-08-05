"""The fleet ledger's staleness rules.

A `⚠ fleet:` warning is a claim about other machines, formed at one instant and
then displayed for hours. These cover the two ways that claim goes wrong: the
local code it was measured against moved on, and the named host got fixed.
"""
import subprocess
import time

from agent_media_core import cli


def _ledger(tmp_path):
    d = tmp_path / "agent-media"
    d.mkdir(parents=True, exist_ok=True)
    return d / "version-skew.log"


def _no_spawn(monkeypatch):
    """Stop the reader's background re-check from actually running doctor —
    it would ssh the real fleet from a unit test."""
    calls = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **kw: calls.append(a) or None)
    return calls


def test_current_verdict_is_shown(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _no_spawn(monkeypatch)
    monkeypatch.setattr(cli, "_local_head_sig", lambda: "agent-media=aaaaaaa")

    _ledger(tmp_path).write_text("# judged agent-media=aaaaaaa\np8ar\n")
    assert "fleet: p8ar" in cli._skew_alert_line()


def test_verdict_judged_against_moved_code_is_discarded(tmp_path, monkeypatch):
    """The deploy race: doctor judged the fleet, then this host moved. Every
    host was compared against code we no longer run, so the verdict describes a
    fleet that no longer exists and must not stay on screen."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    calls = _no_spawn(monkeypatch)
    monkeypatch.setattr(cli, "_local_head_sig", lambda: "agent-media=bbbbbbb")

    _ledger(tmp_path).write_text("# judged agent-media=aaaaaaa\np8ar\n")
    assert cli._skew_alert_line() == ""
    assert calls, "a discarded verdict must trigger an immediate re-check"


def test_unstamped_ledger_still_shows(tmp_path, monkeypatch):
    """A ledger written by an older install carries no stamp. Withholding a
    warning we can't date would be the wrong way to fail."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _no_spawn(monkeypatch)
    monkeypatch.setattr(cli, "_local_head_sig", lambda: "agent-media=bbbbbbb")

    _ledger(tmp_path).write_text("red5!\np8ar\n")
    assert "fleet: red5!, p8ar" in cli._skew_alert_line()


def test_warning_rechecks_sooner_than_a_clean_fleet(tmp_path, monkeypatch):
    """A verdict goes stale the moment the host it names is fixed, and nothing
    tells us that happened. Tonight's false `p8ar` was correct when written and
    obsolete a minute later; two hours is far too long to carry it."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_local_head_sig", lambda: "agent-media=aaaaaaa")
    led = _ledger(tmp_path)

    # Aged past the warning interval but well inside the clean one.
    aged = time.time() - (cli._SKEW_INTERVAL_WARNING_S + 60)
    assert aged > time.time() - cli._SKEW_INTERVAL_CLEAN_S

    calls = _no_spawn(monkeypatch)
    led.write_text("# judged agent-media=aaaaaaa\np8ar\n")
    import os
    os.utime(led, (aged, aged))
    cli._skew_alert_line()
    assert calls, "a displayed warning must be re-checked on the short interval"

    # Same age, but nothing wrong: no re-check owed yet.
    calls.clear()
    led.write_text("# judged agent-media=aaaaaaa\n")
    os.utime(led, (aged, aged))
    assert cli._skew_alert_line() == ""
    assert not calls, "a clean fleet must not be re-checked on the short interval"


def test_repo_head_matches_git(tmp_path):
    """The status bar reads .git directly instead of shelling out; it must
    agree with git itself or the stamp compares the wrong things."""
    from pathlib import Path
    repo = Path.home() / "projects" / "agent-media"
    if not (repo / ".git").exists():
        return
    want = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    assert cli._repo_head("agent-media") == want


def test_missing_repo_is_empty_not_an_error(tmp_path):
    assert cli._repo_head("no-such-repo-here") == ""
