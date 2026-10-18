"""`POST /services/restart`: `media restart-services` on this host, asked by
another one (restart_services.run behind a gated route).

Over real HTTP against the server's own Handler. Every service is fake:
restart_services' enumeration, its restart, its health check, its alert and
its scheduler are patched, so nothing real is restarted or scheduled.
"""
from __future__ import annotations

import os
import threading
from http.server import ThreadingHTTPServer

import pytest

from agent_media_core import restart_services as rs
from agent_media_server import app

from test_contract import call
from test_mic import _device


@pytest.fixture()
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    yield srv.server_address
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def host(monkeypatch):
    """A host with sessiond, a canvas and tts-shim; records what would be
    restarted and scheduled."""
    svcs = [rs.Service("agent-media-sessiond", "up", "systemd"),
            rs.Service("agent-media-visual-canvas", "up", "systemd"),
            rs.Service("agent-media-tts-shim", "up", "systemd")]
    log = {"restarted": [], "scheduled": [], "sessions": [], "own_pid": {}}
    monkeypatch.setattr(rs, "services", lambda: list(svcs))
    monkeypatch.setattr(rs, "_restart_one",
                        lambda s: log["restarted"].append(s.name) or (True, ""))
    monkeypatch.setattr(rs, "health", lambda done, within=None: [])
    monkeypatch.setattr(rs, "alert", lambda *a, **k: None)
    monkeypatch.setattr(rs, "sessiond_sessions", lambda: list(log["sessions"]))
    monkeypatch.setattr(rs, "speech_in_flight", lambda: False)
    monkeypatch.setattr(rs, "_own_cgroup", lambda: "")
    monkeypatch.setattr(rs, "_main_pid", lambda s: log["own_pid"].get(s.name, 0))

    def schedule(selected, after, force):
        log["scheduled"].append(([s.name for s in selected], after, force))
        return {"how": "systemd-run", "unit": "media-restart-test", "log": "-"}

    monkeypatch.setattr(rs, "schedule", schedule)
    return log


@pytest.fixture()
def dev():
    return _device("red5 media CLI")[0]


def _post(addr, body, headers):
    return call(addr, "POST", "/services/restart", body, headers)


def test_it_is_gated(server, host):
    res, got = _post(server, {"only": ["canvas"]}, {})
    assert res.status == 401 and not got["ok"]
    res, _ = _post(server, {"only": ["canvas"]}, {"Authorization": "Bearer nope-not-a-device"})
    assert res.status in (401, 403, 502, 503)
    assert host["restarted"] == [] and host["scheduled"] == []


def test_it_asks_may_admin(server, host, dev, monkeypatch):
    from agent_media_server import auth

    asked = []
    monkeypatch.setattr(auth, "may_admin",
                        lambda b: asked.append(b) or (None, {"status": 403, "error": "no"}))
    res, _ = _post(server, {"only": ["sessiond"]}, dev)
    assert res.status == 403 and asked == [dev["Authorization"].split()[1]]
    assert host["restarted"] == []


def test_a_restart_now_answers_the_run(server, host, dev):
    res, got = _post(server, {"only": ["sessiond", "tts-shim"]}, dev)
    assert res.status == 200 and got["ok"] and got["rc"] == 0
    assert host["restarted"] == ["agent-media-sessiond", "agent-media-tts-shim"]
    assert got["selected"] == host["restarted"]
    assert got["host"] == rs.host() and got["scheduled"] is None
    assert any("restarted" in line for line in got["lines"])
    assert res.getheader("Access-Control-Allow-Origin") == "*"


def test_unknown_names_are_400(server, host, dev):
    res, got = _post(server, {"only": ["sessiond", "nope"]}, dev)
    assert res.status == 400 and got["rc"] == rs.UNKNOWN and got["unknown"] == ["nope"]
    assert host["restarted"] == []


@pytest.mark.parametrize("body", [{"only": [1]}, {"only": {"a": 1}}, {"after": "soon"},
                                  {"after": -5}])
def test_a_bad_body_is_400(server, host, dev, body):
    res, got = _post(server, body, dev)
    assert res.status == 400 and not got["ok"]
    assert host["restarted"] == []


def test_busy_is_409_with_the_reasons(server, host, dev):
    host["sessions"].append({"session": "11111111-aaaa", "state": "working", "live": True})
    res, got = _post(server, {"only": ["sessiond"]}, dev)
    assert res.status == 409 and got["rc"] == rs.BUSY
    assert any("session 11111111" in b and "is working" in b for b in got["busy"])
    assert host["restarted"] == []
    res, got = _post(server, {"only": ["sessiond"], "force": True}, dev)
    assert res.status == 200 and host["restarted"] == ["agent-media-sessiond"]


def test_no_session_is_the_callers(server, host, dev, monkeypatch):
    """A request comes from another host: this process's own
    MEDIA_SESSIOND_SESSION (if any) must not excuse a busy session."""
    monkeypatch.setenv("MEDIA_SESSIOND_SESSION", "11111111-aaaa")
    host["sessions"].append({"session": "11111111-aaaa", "state": "working", "live": True})
    res, got = _post(server, {"only": ["sessiond"], "after": 10}, dev)
    assert res.status == 409 and host["scheduled"] == []


def test_restarting_its_own_server_is_forced_detached(server, host, dev):
    """The canvas is the process answering: restarted now it would die
    before it answered, so it goes 10 s later through the scheduler."""
    host["own_pid"]["agent-media-visual-canvas"] = os.getpid()
    res, got = _post(server, {"only": ["canvas", "tts-shim"]}, dev)
    assert res.status == 200 and got["ok"]
    assert host["restarted"] == []
    assert host["scheduled"] == [(["agent-media-visual-canvas", "agent-media-tts-shim"],
                                  10.0, False)]
    assert got["own"] == ["agent-media-visual-canvas"] and got["after"] == 10.0
    assert got["scheduled"]["unit"] == "media-restart-test"
    # An explicit 0 is no way round it; an explicit delay is kept.
    _post(server, {"only": ["canvas"], "after": 0}, dev)
    _post(server, {"only": ["canvas"], "after": 30}, dev)
    assert [a for _, a, _ in host["scheduled"][1:]] == [10.0, 30.0]
    assert host["restarted"] == []


def test_own_service_found_through_a_parent_and_the_cgroup(host, monkeypatch):
    """The service's main pid may be a wrapper that started this python
    (runit's run script), or systemd's cgroup names it."""
    svc = rs.Service("media-visual-canvas", "up", "runit")
    other = rs.Service("media-sessiond", "up", "runit")
    host["own_pid"]["media-visual-canvas"] = os.getppid()
    assert rs.own_services([svc, other]) == [svc]
    host["own_pid"].clear()
    assert rs.own_services([svc]) == []
    sd = rs.Service("agent-media-visual-canvas", "up", "systemd")
    monkeypatch.setattr(rs, "_own_cgroup",
                        lambda: "0::/user.slice/.../app.slice/agent-media-visual-canvas.service\n")
    assert rs.own_services([sd]) == [sd]


def test_a_restart_of_other_services_is_not_delayed(server, host, dev):
    host["own_pid"]["agent-media-visual-canvas"] = os.getpid()
    res, got = _post(server, {"only": ["sessiond"]}, dev)
    assert res.status == 200 and host["restarted"] == ["agent-media-sessiond"]
    assert host["scheduled"] == [] and got["own"] == []


def test_dry_run_touches_nothing_and_answers_200_with_the_checks(server, host, dev):
    host["own_pid"]["agent-media-visual-canvas"] = os.getpid()
    host["sessions"].append({"session": "22222222-bbbb", "state": "approval",
                             "pending": [{"request_id": "r"}], "live": True})
    res, got = _post(server, {"only": ["sessiond", "canvas"], "dry_run": True}, dev)
    assert res.status == 200 and got["rc"] == rs.BUSY and not got["ok"]
    assert any("approval(s) pending" in b for b in got["busy"])
    assert any("would schedule in 10 s" in line for line in got["lines"])
    assert host["restarted"] == [] and host["scheduled"] == []


def test_a_failed_restart_is_500(server, host, dev, monkeypatch):
    monkeypatch.setattr(rs, "_restart_one", lambda s: (False, "boom"))
    res, got = _post(server, {"only": ["tts-shim"]}, dev)
    assert res.status == 500 and got["rc"] == rs.FAILED
    assert any("restart FAILED: boom" in line for line in got["lines"])
