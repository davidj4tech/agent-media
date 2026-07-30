"""Runtime health checks — `media selfcheck` and the doctor probe.

These exist because version skew was the only thing `media doctor` ever
checked. On 2026-07-30 the phone sat on the correct commit while every
entrypoint raised ModuleNotFoundError (Termux upgraded python and stranded
site-packages) and media-mcp crash-looped ~1250 times unnoticed. A current
checkout says nothing about whether the code can actually run.
"""

import subprocess
import time
from pathlib import Path

from agent_media_core import cli


def test_parse_selfcheck_reads_key_value_lines():
    facts = cli.parse_selfcheck(
        "selfcheck=1\npython=3.14\ninstall=editable\n\ngarbage line\ndown=media-mcp\n")
    assert facts["python"] == "3.14"
    assert facts["install"] == "editable"
    assert facts["down"] == "media-mcp"
    assert "garbage line" not in facts


def test_health_problems_flags_a_dead_install():
    problems = cli.health_problems({"selfcheck": "broken"})
    assert len(problems) == 1
    assert "will not import" in problems[0]


def test_health_problems_flags_a_non_editable_copy():
    """The quiet one: everything runs, but `git pull` deploys nothing — how
    call-guard on the phone ran weeks-old code."""
    problems = cli.health_problems({"selfcheck": "1", "install": "copy"})
    assert any("not editable" in p for p in problems)


def test_health_problems_reports_down_and_looping_services():
    problems = cli.health_problems(
        {"install": "editable", "down": "media-mcp,book-observer",
         "crashloop": "media-mcp:37"})
    joined = " ".join(problems)
    assert "media-mcp" in joined and "book-observer" in joined
    assert "37" in joined


def test_health_problems_silent_on_a_healthy_host():
    assert cli.health_problems(
        {"selfcheck": "1", "install": "editable", "services": "5"}) == []


def test_health_problems_tolerates_an_older_host():
    """A host too old to have `selfcheck` must not be reported as broken —
    skew reporting still covers it."""
    assert cli.health_problems({"selfcheck": "unsupported"}) == []


def test_selfcheck_facts_report_this_editable_checkout(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    facts = cli.selfcheck_facts()
    # The test suite runs from a git checkout with an editable install, which
    # is exactly the healthy shape.
    assert facts["install"] == "editable"
    assert facts["module"].endswith("agent_media_core/__init__.py")
    assert cli.health_problems(facts) == []


def test_crash_loops_counts_recent_failures_only(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    d = tmp_path / "agent-media" / "sv-crash"
    d.mkdir(parents=True)
    now = time.time()
    (d / "media-mcp.log").write_text(
        f"{now - 10} 1\n{now - 20} 1\n{now - 99999} 1\n")   # third is ancient
    (d / "quiet.log").write_text(f"{now - 99999} 1\n")
    loops = dict(cli._crash_loops())
    assert loops == {"media-mcp": 2}


CRASH_NOTIFY = (Path(__file__).resolve().parents[1]
                / "services" / "_common" / "crash-notify")


def _run_crash_notify(state_home, svc="demo"):
    t0 = time.monotonic()
    subprocess.run([str(CRASH_NOTIFY), svc, "1", "x"], check=False,
                   env={"HOME": str(state_home), "PATH": "/usr/bin:/bin",
                        "XDG_STATE_HOME": str(state_home),
                        "MEDIA_NOTIFY_DISABLED": "1"},
                   capture_output=True, timeout=60)
    return time.monotonic() - t0


def test_crash_notify_backs_off_only_once_looping(tmp_path):
    """runit restarts instantly, so a service dying on startup spins as fast as
    the OS allows (media-mcp: ~1250 overnight). Ordinary restarts stay fast;
    the third failure inside a minute starts costing 10s."""
    assert CRASH_NOTIFY.exists()
    assert _run_crash_notify(tmp_path) < 5      # 1st: no backoff
    assert _run_crash_notify(tmp_path) < 5      # 2nd: still an ordinary restart
    assert _run_crash_notify(tmp_path) >= 9     # 3rd: crash loop -> back off

    # And it left the ledger `media doctor` reads.
    log = tmp_path / "agent-media" / "sv-crash" / "demo.log"
    assert len(log.read_text().strip().splitlines()) == 3
