"""`media restart-services`: --only, the busy checks, --after, the health check.

Every service here is fake. `systemctl`, `systemd-run` and `agent-alert` are
scripts on PATH that log their arguments; sessiond is its records on disk
(its socket is absent, so restart_services reads them, as it does when sessiond
is down); speech is a patched `_speech_in_flight`. Nothing real is restarted.
"""
from __future__ import annotations

import http.server
import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from agent_media_core import cli, restart_services as rs

UNITS = ["agent-media-sessiond", "agent-media-visual-canvas", "agent-media-tts-shim",
         "agent-media-mcp", "agent-media-feed-gc"]

FAKE_SYSTEMCTL = r"""#!/bin/sh
echo "systemctl $*" >> "$FAKE_LOG"
case "$2" in
  list-units)
    for u in $FAKE_UNITS; do echo "$u.service loaded active running fake"; done
    echo "agent-media-feed-gc.timer loaded active waiting fake" ;;
  restart) exit 0 ;;
  is-active)
    for d in $FAKE_DOWN; do [ "$3" = "$d.service" ] && { echo failed; exit 3; }; done
    echo active ;;
esac
"""

FAKE_LOGGER = """#!/bin/sh
echo "$(basename "$0") $*" >> "$FAKE_LOG"
"""


def _script(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def host(tmp_path, monkeypatch):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    _script(bin_ / "systemctl", FAKE_SYSTEMCTL)
    _script(bin_ / "systemd-run", FAKE_LOGGER)
    _script(bin_ / "agent-alert", FAKE_LOGGER)
    log = tmp_path / "calls.log"
    log.touch()
    for k in list(os.environ):
        if k.startswith(("MEDIA_SESSIOND", "MEDIA_RESTART", "MEDIA_CANVAS", "MEDIA_VISUAL",
                         "AGENT_ALERT")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("MEDIA_SESSIOND_SOCKET", str(tmp_path / "no-sessiond.sock"))
    srv = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    threading.Thread(target=srv.serve_forever, args=(0.01,), daemon=True).start()
    monkeypatch.setenv("MEDIA_CANVAS_URL", f"http://127.0.0.1:{srv.server_port}/")
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_UNITS", " ".join(UNITS))
    monkeypatch.setenv("FAKE_DOWN", "")
    monkeypatch.setenv("MEDIA_RESTART_HEALTH_S", "0")
    monkeypatch.setattr(cli, "_sv_dir", lambda: None)
    monkeypatch.setattr(cli, "_speech_in_flight", lambda: False)

    class Host:
        path = tmp_path

        @staticmethod
        def calls() -> list[str]:
            return log.read_text().splitlines()

        @staticmethod
        def restarts() -> list[str]:
            return [c.split()[-1] for c in log.read_text().splitlines()
                    if c.startswith("systemctl --user restart")]

        @staticmethod
        def alerts() -> list[str]:
            return [c for c in log.read_text().splitlines() if c.startswith("agent-alert")]

        @staticmethod
        def session(sid: str, **rec) -> None:
            pytest.importorskip("agent_media_server.sessiond")
            d = tmp_path / "state" / "agent-media" / "sessiond"
            d.mkdir(parents=True, exist_ok=True)
            base = {"session": sid, "state": "waiting", "pid": os.getpid(), "pending": [],
                    "queued": [], "tasks": 0, "first_text": "a chat"}
            (d / f"{sid}.json").write_text(json.dumps({**base, **rec}))

    yield Host
    srv.shutdown()


def _run(*argv: str) -> int:
    return cli.main(["restart-services", *argv])


def test_only_short_names_restart_just_those(host):
    assert _run("--only", "sessiond", "canvas") == 0
    assert host.restarts() == ["agent-media-sessiond.service",
                               "agent-media-visual-canvas.service"]


def test_no_only_restarts_everything_it_owns(host):
    assert _run() == 0
    assert set(host.restarts()) == {f"{u}.service" for u in UNITS}


def test_unknown_name_exits_2_listing_names(host, capsys):
    assert _run("--only", "sessiond", "nope") == 2
    out = capsys.readouterr().out
    assert "unknown service(s): nope" in out and "canvas" in out and "tts-shim" in out
    assert host.restarts() == []


def test_dry_run_touches_nothing_and_shows_the_checks(host, capsys):
    assert _run("--dry-run", "--only", "sessiond", "--after", "10") == 0
    out = capsys.readouterr().out
    assert "would restart" in out and "busy checks: clear" in out
    assert "would schedule in 10 s" in out
    assert host.restarts() == [] and not any("systemd-run" in c for c in host.calls())



@pytest.mark.parametrize("rec,why", [
    ({"state": "working"}, "is working"),
    ({"state": "approval", "pending": [{"request_id": "r1"}]}, "approval(s) pending"),
    ({"queued": ["c1"]}, "queued message"),
    ({"tasks": 2}, "task(s) in flight"),
])
def test_sessiond_refused_while_another_session_is_busy(host, capsys, rec, why):
    host.session("11111111-aaaa", **rec)
    assert _run("--only", "sessiond") == 3
    out = capsys.readouterr().out
    assert why in out and "session 11111111" in out
    assert host.restarts() == []


def test_force_skips_the_checks(host):
    host.session("11111111-aaaa", state="working")
    assert _run("--only", "sessiond", "--force") == 0
    assert host.restarts() == ["agent-media-sessiond.service"]


def test_dry_run_reports_busy_with_exit_3(host, capsys):
    host.session("11111111-aaaa", state="working")
    assert _run("--dry-run", "--only", "sessiond") == 3
    assert "busy: sessiond: session 11111111" in capsys.readouterr().out


def test_a_dead_sessions_record_does_not_count(host):
    host.session("11111111-aaaa", state="working", pid=2 ** 22 + 12345)
    assert _run("--only", "sessiond") == 0


def test_idle_sessions_do_not_block_and_other_services_ignore_sessiond(host):
    host.session("11111111-aaaa", state="waiting")
    assert _run("--only", "sessiond") == 0
    host.session("22222222-bbbb", state="working")
    assert _run("--only", "mcp") == 0


def test_callers_own_session_is_left_out(host, monkeypatch):
    """The caller is working (it is running this command) and has its one
    task: that is not busy. It may not restart sessiond *now*, though."""
    monkeypatch.setenv("MEDIA_SESSIOND_SESSION", "33333333-cccc")
    host.session("33333333-cccc", state="working", tasks=1)
    assert _run("--only", "sessiond") == 3   # now would end this chat
    assert _run("--only", "sessiond", "--after", "10") == 0
    assert any(c.startswith("systemd-run") for c in host.calls())


def test_callers_other_tasks_refuse(host, monkeypatch, capsys):
    monkeypatch.setenv("MEDIA_SESSIOND_SESSION", "33333333-cccc")
    host.session("33333333-cccc", state="working", tasks=3)
    assert _run("--only", "sessiond", "--after", "10") == 3
    assert "your own session has 2 other task(s)" in capsys.readouterr().out


def test_speech_services_refused_while_speaking(host, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_speech_in_flight", lambda: True)
    assert _run("--only", "tts-shim") == 3
    assert "a reply is being spoken" in capsys.readouterr().out
    assert _run("--only", "mcp") == 0   # not a speech service


def test_after_schedules_a_transient_unit_and_returns(host, capsys, monkeypatch):
    monkeypatch.setenv("MEDIA_SESSIOND_SESSION", "33333333-cccc")
    assert _run("--only", "sessiond", "--after", "10") == 0
    out = capsys.readouterr().out
    assert "scheduled: agent-media-sessiond in 10 s (systemd-run media-restart-" in out
    run = next(c for c in host.calls() if c.startswith("systemd-run"))
    assert "--user" in run and "--on-active=10" in run and "--collect" in run
    assert "-m agent_media_core.restart_services --fire --only agent-media-sessiond" in run
    assert "MEDIA_SESSIOND_SESSION" not in run
    assert host.restarts() == []


def test_after_without_systemd_detaches_a_child(host, monkeypatch, capsys):
    """runit (the phone) and Windows: one child in its own session, which
    sleeps itself and logs to the state dir."""
    monkeypatch.setattr(cli, "_systemd_service_states", lambda: [])
    monkeypatch.setattr(cli, "_runit_service_states",
                        lambda root: [("media-sessiond", "up")])
    seen = {}

    class P:
        pid = 4242

    def popen(argv, **kw):
        seen.update(argv=argv, **kw)
        return P()

    monkeypatch.setattr(rs.subprocess, "Popen", popen)
    monkeypatch.setenv("MEDIA_SESSIOND_SESSION", "33333333-cccc")
    assert _run("--only", "sessiond", "--after", "10") == 0
    assert seen["argv"][:3] == [sys.executable, "-m", "agent_media_core.restart_services"]
    assert seen["env"]["MEDIA_RESTART_DELAY_S"] == "10"
    assert "MEDIA_SESSIOND_SESSION" not in seen["env"]
    assert seen.get("start_new_session") or seen.get("creationflags")
    assert "detached pid 4242" in capsys.readouterr().out


def test_fire_waits_out_a_busy_host_then_restarts(host, monkeypatch):
    host.session("11111111-aaaa", state="working")
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        host.session("11111111-aaaa", state="waiting")

    monkeypatch.setattr(rs, "_sleep", sleep)
    assert rs.main(["--fire", "--only", "agent-media-sessiond"]) == 0
    assert sleeps == [15.0]
    assert host.restarts() == ["agent-media-sessiond.service"]


def test_fire_gives_up_after_the_wait_and_alerts(host, monkeypatch):
    host.session("11111111-aaaa", state="working")
    monkeypatch.setattr(rs, "_sleep", lambda s: None)
    assert rs.main(["--fire", "--only", "agent-media-sessiond"]) == 3
    assert host.restarts() == []
    (a,) = host.alerts()
    assert "restart-services." in a and "--level warn" in a and "gave up" in a


def test_fire_counts_the_callers_session_too(host, monkeypatch):
    """By fire time the caller's turn should be over; if it is not, wait for
    it rather than cut it off."""
    monkeypatch.setenv("MEDIA_SESSIOND_SESSION", "33333333-cccc")
    host.session("33333333-cccc", state="working", tasks=1)
    monkeypatch.setattr(rs, "_sleep", lambda s: None)
    assert rs.main(["--fire", "--only", "agent-media-sessiond"]) == 3


def test_health_failure_raises_the_host_alert(host, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_DOWN", "agent-media-mcp")
    assert _run("--only", "mcp") == 1
    assert "health check FAILED" in capsys.readouterr().out
    (a,) = host.alerts()
    assert "--level warn" in a and "agent-media-mcp" in a


def test_clean_restart_reports_ok_quietly(host):
    assert _run("--only", "mcp") == 0
    (a,) = host.alerts()
    assert "--level ok" in a and "--quiet" in a


def test_timer_jobs_are_not_health_checked(host, monkeypatch):
    monkeypatch.setenv("FAKE_DOWN", "agent-media-feed-gc")
    assert _run("--only", "feed-gc") == 0


def test_canvas_must_answer_http(host, monkeypatch, capsys):
    assert _run("--only", "canvas") == 0          # a 501 is still an answer
    monkeypatch.setenv("MEDIA_CANVAS_URL", "http://127.0.0.1:9/")   # nothing there
    assert _run("--only", "canvas") == 1
    assert "not answering HTTP" in capsys.readouterr().out
