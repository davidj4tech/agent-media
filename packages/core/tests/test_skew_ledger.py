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

    _ledger(tmp_path).write_text("# judged agent-media=aaaaaaa\np8a\n")
    assert "fleet: p8a" in cli._skew_alert_line()


def test_verdict_judged_against_moved_code_is_discarded(tmp_path, monkeypatch):
    """The deploy race: doctor judged the fleet, then this host moved. Every
    host was compared against code we no longer run, so the verdict describes a
    fleet that no longer exists and must not stay on screen."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    calls = _no_spawn(monkeypatch)
    monkeypatch.setattr(cli, "_local_head_sig", lambda: "agent-media=bbbbbbb")

    _ledger(tmp_path).write_text("# judged agent-media=aaaaaaa\np8a\n")
    assert cli._skew_alert_line() == ""
    assert calls, "a discarded verdict must trigger an immediate re-check"


def test_unstamped_ledger_still_shows(tmp_path, monkeypatch):
    """A ledger written by an older install carries no stamp. Withholding a
    warning we can't date would be the wrong way to fail."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _no_spawn(monkeypatch)
    monkeypatch.setattr(cli, "_local_head_sig", lambda: "agent-media=bbbbbbb")

    _ledger(tmp_path).write_text("red5!\np8a\n")
    assert "fleet: red5!, p8a" in cli._skew_alert_line()


def test_warning_rechecks_sooner_than_a_clean_fleet(tmp_path, monkeypatch):
    """A verdict goes stale the moment the host it names is fixed, and nothing
    tells us that happened. Tonight's false `p8a` was correct when written and
    obsolete a minute later; two hours is far too long to carry it."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_local_head_sig", lambda: "agent-media=aaaaaaa")
    led = _ledger(tmp_path)

    # Aged past the warning interval but well inside the clean one.
    aged = time.time() - (cli._SKEW_INTERVAL_WARNING_S + 60)
    assert aged > time.time() - cli._SKEW_INTERVAL_CLEAN_S

    calls = _no_spawn(monkeypatch)
    led.write_text("# judged agent-media=aaaaaaa\np8a\n")
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


# --- where the verdict goes on the line ------------------------------------
#
# It used to replace the speech status outright. That is right for a lost
# reply — a frozen bar reads as playback, so the fault has to take the line —
# and wrong for the fleet, which is a claim about some other machine while the
# words being spoken here are fine. A complaint nobody can act on from where
# they are standing then blanks the bar speech is watched on, for days.

import argparse
import io
from contextlib import redirect_stdout


def _status(monkeypatch, playing, **kw):
    args = argparse.Namespace(width=60, show_idle=False, no_bar=False,
                              title=None, now_playing=False)
    monkeypatch.setattr(cli, "_miss_alert_line", lambda: "")
    monkeypatch.setattr(cli, "_speech_display_state",
                        lambda **_kw: playing)
    monkeypatch.setattr(cli, "_speech_visual_flag", lambda: False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.cmd_status(args)
    return buf.getvalue().strip()


_PLAYING = (False, 3.0, 11.0, False, False, 1.0, True)
_IDLE = (True, None, None, False, False, None, False)


def test_the_reply_keeps_the_line_and_the_fleet_gets_a_mark(monkeypatch):
    monkeypatch.setattr(cli, "_fleet_alert_entries", lambda: ["p8a!"])
    monkeypatch.setattr(cli, "_alert_glyph", lambda: "⚠")
    out = _status(monkeypatch, _PLAYING)
    assert "00:03" in out and "00:11" in out, (
        "a fleet warning blanked the progress of a reply that was playing")
    assert out.endswith("⚠")


def test_with_nothing_playing_the_hosts_are_named(monkeypatch):
    monkeypatch.setattr(cli, "_fleet_alert_entries", lambda: ["p8a!", "red5"])
    monkeypatch.setattr(cli, "_alert_glyph", lambda: "⚠")
    out = _status(monkeypatch, _IDLE)
    assert out == "⚠ fleet: p8a!, red5"


def test_a_healthy_fleet_adds_nothing(monkeypatch):
    monkeypatch.setattr(cli, "_fleet_alert_entries", lambda: [])
    out = _status(monkeypatch, _PLAYING)
    assert "⚠" not in out and "00:11" in out


def test_a_lost_reply_still_takes_the_line(monkeypatch):
    """The one alert that must keep replacing: a bar beside it would read as
    playback for a reply that never arrived."""
    monkeypatch.setattr(cli, "_fleet_alert_entries", lambda: ["p8a!"])
    monkeypatch.setattr(cli, "_speech_display_state", lambda **_kw: _PLAYING)
    monkeypatch.setattr(cli, "_miss_alert_line", lambda: "⚠ app unreachable (2 lost)")
    args = argparse.Namespace(width=60, show_idle=False, no_bar=False,
                              title=None, now_playing=False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.cmd_status(args)
    assert buf.getvalue().strip() == "⚠ app unreachable (2 lost)"
