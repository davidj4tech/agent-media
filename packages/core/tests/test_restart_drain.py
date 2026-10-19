"""`media restart-services` draining sessiond (19 Oct 2026): a busy host is
not refused but waited for, with a pending-restart record sessiond and the
context hook read; the detached run fires the moment nothing is busy, or
gives up at the deadline.

The same fakes as test_restart_services.py (its `host` fixture): fake
`systemctl`/`systemd-run`/`agent-alert` on PATH, sessiond as records on disk
in a throwaway state dir. Nothing real is restarted or held.
"""
from __future__ import annotations

import json

import pytest

from agent_media_core import restart_services as rs
from agent_media_core.intake import context

from test_restart_services import _run, host  # noqa: F401 — the fixture


def test_after_drains_a_busy_sessiond_instead_of_refusing(host, capsys):
    host.session("11111111-aaaa", state="working")
    assert _run("--only", "sessiond", "--after", "10") == 0
    out = capsys.readouterr().out
    assert "waiting on: sessiond: session 11111111" in out and "draining until" in out
    run = next(c for c in host.calls() if c.startswith("systemd-run"))
    assert "--fire --only agent-media-sessiond --drain" in run
    rec = rs.pending()
    assert rec and rec["state"] == "waiting" and rec["only"] == ["agent-media-sessiond"]
    assert [w["session"] for w in rec["waiting_on"]] == ["11111111-aaaa"]
    assert rec["deadline"] - rec["since"] == pytest.approx(10 + 1800, abs=1)
    assert host.restarts() == []


def test_dry_run_says_it_would_drain_and_answers_as_the_run_would(host, capsys):
    host.session("11111111-aaaa", state="working")
    assert _run("--dry-run", "--only", "sessiond", "--after", "10") == 0
    assert "would drain" in capsys.readouterr().out
    assert rs.read_pending() is None
    assert _run("--dry-run", "--only", "sessiond", "--after", "10", "--no-drain") == 3


def test_no_drain_and_now_still_refuse(host):
    host.session("11111111-aaaa", state="working")
    assert _run("--only", "sessiond", "--after", "10", "--no-drain") == 3
    assert _run("--only", "sessiond") == 3
    assert rs.read_pending() is None


def test_a_second_request_joins_the_pending_one(host, capsys):
    host.session("11111111-aaaa", state="working")
    assert _run("--only", "sessiond", "--after", "10") == 0
    assert _run("--only", "sessiond", "canvas", "--after", "10") == 0
    assert "joined it" in capsys.readouterr().out
    assert sum(c.startswith("systemd-run") for c in host.calls()) == 1
    assert rs.pending()["only"] == ["agent-media-sessiond", "agent-media-visual-canvas"]


def test_cancel_drops_it(host, capsys):
    host.session("11111111-aaaa", state="working")
    _run("--only", "sessiond", "--after", "10")
    assert _run("--cancel") == 0
    assert "cancelled the restart" in capsys.readouterr().out
    assert rs.read_pending() is None


def test_fire_drain_restarts_the_moment_nothing_is_busy(host, monkeypatch):
    host.session("11111111-aaaa", state="working")
    _run("--only", "sessiond", "--after", "10")
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        # Still busy on the first check: the record says on what.
        assert rs.pending()["waiting_on"][0]["session"] == "11111111-aaaa"
        host.session("11111111-aaaa", state="waiting")

    monkeypatch.setattr(rs, "_sleep", sleep)
    assert rs.main(["--fire", "--only", "agent-media-sessiond", "--drain"]) == 0
    assert sleeps == [3.0]
    assert host.restarts() == ["agent-media-sessiond.service"]
    assert rs.read_pending() is None


def test_a_session_only_waiting_on_a_question_does_not_hold_a_drain(host, monkeypatch):
    host.session("11111111-aaaa", state="approval", pending=[{"request_id": "r1"}])
    assert _run("--only", "sessiond", "--after", "10") == 0
    assert rs.pending()["waiting_on"] == []
    monkeypatch.setattr(rs, "_sleep", lambda s: None)
    assert rs.main(["--fire", "--only", "agent-media-sessiond", "--drain"]) == 0
    assert host.restarts() == ["agent-media-sessiond.service"]
    # Without a drain a card still refuses, as before.
    assert _run("--only", "sessiond") == 3


def test_a_question_with_work_in_flight_still_holds_it(host):
    host.session("11111111-aaaa", state="approval", pending=[{"request_id": "r1"}], tasks=1)
    _run("--only", "sessiond", "--after", "10")
    assert [w["kind"] for w in rs.pending()["waiting_on"]] == ["question"]


def test_fire_drain_gives_up_at_the_deadline_releasing_and_alerting(host, monkeypatch):
    monkeypatch.setenv("MEDIA_RESTART_DRAIN_MAX_S", "0")
    host.session("11111111-aaaa", state="working")
    _run("--only", "sessiond", "--after", "0.001")
    rec = rs.read_pending()
    rs._update_pending(lambda r: {**r, "deadline": r["since"] - 1})
    monkeypatch.setattr(rs, "_sleep", lambda s: None)
    assert rec is not None
    assert rs.main(["--fire", "--only", "agent-media-sessiond", "--drain"]) == 3
    assert host.restarts() == [] and rs.read_pending() is None
    (a,) = host.alerts()
    assert "--level warn" in a and "gave up" in a and "draining" in a


def test_a_pending_restart_whose_run_went_quiet_holds_nothing(host):
    host.session("11111111-aaaa", state="working")
    _run("--only", "sessiond", "--after", "10")
    rs._update_pending(lambda r: {**r, "alive_until": r["since"] - 1})
    assert rs.read_pending() is not None and rs.pending() is None


def test_fire_drain_stops_when_cancelled(host, monkeypatch):
    host.session("11111111-aaaa", state="working")
    _run("--only", "sessiond", "--after", "10")
    monkeypatch.setattr(rs, "_sleep", lambda s: rs.clear_pending())
    assert rs.main(["--fire", "--only", "agent-media-sessiond", "--drain"]) == 3
    assert host.restarts() == []


# --- the note a waited-on session is told -------------------------------------------

def _hook(sid="11111111-aaaa", **kw):
    return {"hook_event_name": "PostToolUse", "session_id": sid, **kw}


def test_the_restart_note_is_told_once_to_a_waited_on_session(host, monkeypatch):
    host.session("11111111-aaaa", state="working")
    host.session("22222222-bbbb", state="waiting")
    _run("--only", "sessiond", "--after", "10")
    monkeypatch.setenv("MEDIA_SESSIOND_SESSION", "11111111-aaaa")
    out = json.loads(context.restart_note_for(_hook()))
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "end your turn" in out["hookSpecificOutput"]["additionalContext"]
    assert context.restart_note_for(_hook()) == ""                # once
    # Not a subagent, not a session it is not waiting on, not a pane.
    monkeypatch.setenv("MEDIA_SESSIOND_SESSION", "22222222-bbbb")
    assert context.restart_note_for(_hook("22222222-bbbb")) == ""
    monkeypatch.delenv("MEDIA_SESSIOND_SESSION")
    assert context.restart_note_for(_hook()) == ""


def test_a_subagent_is_not_told(host, monkeypatch):
    host.session("11111111-aaaa", state="working")
    _run("--only", "sessiond", "--after", "10")
    monkeypatch.setenv("MEDIA_SESSIOND_SESSION", "11111111-aaaa")
    assert context.restart_note_for(_hook(agent_id="a1")) == ""
    assert context.restart_note_for(_hook()) != ""


def test_the_prompt_hook_carries_it_too(host, monkeypatch):
    host.session("11111111-aaaa", state="working")
    _run("--only", "sessiond", "--after", "10")
    monkeypatch.setenv("MEDIA_SESSIOND_SESSION", "11111111-aaaa")
    monkeypatch.setenv("MEDIA_AGENT_NOTES", "0")
    monkeypatch.setenv("MEDIA_THREAD_LINK", "0")
    note = context.note_for({"hook_event_name": "UserPromptSubmit",
                             "session_id": "11111111-aaaa"})
    assert "A restart of the session host is waiting" in note


def test_setup_installs_the_post_tool_use_hook(tmp_path):
    from agent_media_core import setup

    path = tmp_path / "settings.json"
    path.write_text("{}")
    args = type("A", (), {"settings": str(path), "config_dir": None, "dry_run": False})()
    assert setup._row_context(args, check_only=False)[0] == "installed"
    hooks = json.loads(path.read_text())["hooks"]
    (cmd,) = [h["command"] for g in hooks["PostToolUse"] for h in g["hooks"]]
    assert cmd.startswith('[ -n "$MEDIA_SESSIOND_SESSION" ]') and "--restart" in cmd
    assert "restart-pending.json" in cmd
    assert setup._row_context(args, check_only=True)[0] == "ok"
    assert setup._row_context(args, check_only=False)[0] == "ok"
