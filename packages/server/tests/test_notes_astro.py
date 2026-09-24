"""astro.org kept a year ahead (notes-paragtd's astro.py): which years are
missing, the generator run for them, and the setup row that turns the monthly
timer on. The generator and systemd are stubbed: nothing here runs either."""

from __future__ import annotations

import datetime as dt
import subprocess

import pytest

from agent_media_notes_paragtd import astro
from agent_media_server import auth, notes, notes_profile, notes_setup


@pytest.fixture()
def org(tmp_path, monkeypatch):
    root = tmp_path / "org"
    root.mkdir()
    (root / "inbox.org").write_text("#+title: Inbox\n")
    (root / "astro.org").write_text("#+title: Astro\n* 2026 Lunar Routines\n* 2026 Decan Alerts\n")
    gen = tmp_path / "paragtd" / "bin" / "paragtd-astro-generate"
    gen.parent.mkdir(parents=True)
    gen.write_text("#!/bin/sh\n")
    monkeypatch.setenv("MEDIA_PARAGTD_DIR", str(tmp_path / "paragtd"))
    monkeypatch.setenv("MEDIA_NOTES_DIR", str(root))
    monkeypatch.setenv("MEDIA_NOTES_PROFILE", "paragtd")
    monkeypatch.setenv("MEDIA_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(auth, "gate", lambda b: ({"username": "david"}, {}))
    monkeypatch.setattr(auth, "may_control_speech", lambda b: (True, {}))
    monkeypatch.setattr(notes, "_memory_call", lambda *a, **k: None)
    monkeypatch.setattr(notes_setup, "_systemctl", lambda *a: (127, "no systemctl here"))
    notes_profile._reset_for_tests()
    return root


def _fake_run(calls):
    def run(cmd, **kw):
        calls.append(cmd)
        year = cmd[cmd.index("--year") + 1]
        with open(cmd[cmd.index("--output") + 1], "a") as f:
            f.write(f"* {year} Lunar Routines\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return run


def test_the_missing_year_is_added(org):
    calls: list = []
    added = astro.ensure(org, dt.date(2026, 9, 24), run=_fake_run(calls))
    assert added == [2027] and astro.years(org) == [2026, 2027]
    assert calls[0][1:] == ["--year", "2027", "--output", str(org / "astro.org"),
                            "--timezone", "Australia/Melbourne", "--append"]
    assert astro.ensure(org, dt.date(2026, 12, 31), run=_fake_run(calls)) == []


def test_a_fresh_tree_gets_both_years_without_append(org):
    (org / "astro.org").unlink()
    calls: list = []
    assert astro.ensure(org, dt.date(2026, 9, 24), run=_fake_run(calls)) == [2026, 2027]
    assert "--append" not in calls[0] and "--append" in calls[1]


def test_a_failing_generator_says_so(org):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "no kerykeion")
    with pytest.raises(RuntimeError, match="kerykeion"):
        astro.ensure(org, dt.date(2026, 9, 24), run=run)


def test_setup_turns_the_timer_on(org, tmp_path, monkeypatch):
    enabled: list = []
    monkeypatch.setattr(astro, "_systemctl", lambda *a: (
        enabled.append(a) or (0, "enabled") if a[0] != "is-enabled" or enabled else (1, "disabled")))
    monkeypatch.setattr(astro, "ensure", lambda root: [2027])
    rows = {r["name"]: r for r in notes_setup.status("good")[1]["components"]}
    assert rows["astro"]["state"] == "off" and rows["astro"]["actions"] == ["enable", "run"]
    assert rows["astro"]["detail"].startswith("astro.org has 2026")
    ok, got = notes_setup.run("astro", "enable", "good")
    assert ok and got["added"] == [2027]
    unit = (tmp_path / "config" / "systemd" / "user" / "paragtd-astro.service").read_text()
    assert f"MEDIA_NOTES_DIR={org}" in unit and "-m agent_media_notes_paragtd.astro" in unit
    assert "OnCalendar=monthly" in (tmp_path / "config" / "systemd" / "user" / "paragtd-astro.timer").read_text()
    assert ("enable", "--now", "paragtd-astro.timer") in enabled


def test_no_generator_no_timer(org, tmp_path):
    (tmp_path / "paragtd" / "bin" / "paragtd-astro-generate").unlink()
    rows = {r["name"]: r for r in notes_setup.status("good")[1]["components"]}
    assert rows["astro"]["state"] == "missing" and rows["astro"]["actions"] == []
