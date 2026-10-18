"""`media restart-services --host NAME` and `media peer pair`: another host's
services restarted through its own server, as a paired device of it.

The other host is a fake HTTP server on 127.0.0.1 (its `/pair` and
`/services/restart` record what they were sent and answer what a test sets),
and once the real server Handler with every service faked. HOME and the
config are throwaway: no real peer, token or phone is touched.
"""
from __future__ import annotations

import http.server
import json
import os
import stat
import threading

import pytest

from agent_media_core import cli, peer_link, restart_services as rs


class Fake:
    def __init__(self):
        self.seen: list[tuple[str, dict, dict]] = []
        self.answer: tuple[int, dict] = (200, {})
        self.token = "tok-from-the-phone"


@pytest.fixture
def phone(tmp_path, monkeypatch):
    fake = Fake()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            fake.seen.append((self.path, dict(self.headers), body))
            if self.path == "/pair":
                code, out = ((200, {"ok": True, "token": fake.token, "device_id": "d1",
                                    "name": body.get("device"),
                                    "server": {"name": "p8a"}})
                             if body.get("code") == "c0de" else (403, {"ok": False}))
            else:
                code, out = fake.answer
            raw = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, args=(0.01,), daemon=True).start()
    fake.url = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "config.toml"
    cfg.write_text('[peers.phone]\nhost = "p8a"\nroles = ["observe"]\n')
    monkeypatch.setenv("MEDIA_CONFIG", str(cfg))
    # No server of this host's own to ask first (run_device): a closed port,
    # never the real canvas.
    monkeypatch.setenv("MEDIA_PHONE_JOBS_URL", "http://127.0.0.1:9")
    yield fake
    srv.shutdown()
    srv.server_close()


def _pair(fake) -> int:
    link = f"sasonica://pair?server={fake.url.replace(':', '%3A').replace('/', '%2F')}&code=c0de"
    return cli.main(["peer", "pair", "phone", link])


def test_pair_keeps_the_token_and_url_0600(phone, capsys):
    assert _pair(phone) == 0
    path, _, body = phone.seen[0]
    assert path == "/pair" and body["code"] == "c0de" and body["device"].endswith(" media")
    f = peer_link._file("phone")
    rec = json.loads(f.read_text())
    assert rec["token"] == phone.token and rec["url"] == phone.url
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert "paired with phone" in capsys.readouterr().out
    # By host name too: the alias is the key.
    assert peer_link.load("p8a")["token"] == phone.token


def test_a_bad_code_is_refused_and_nothing_kept(phone, capsys):
    assert cli.main(["peer", "pair", "phone", "nope", "--server", phone.url]) == 1
    assert "said 403" in capsys.readouterr().err
    assert peer_link.load("phone") is None


def test_list_shows_paired_or_not(phone, capsys):
    assert cli.main(["peer", "list"]) == 0
    assert "not paired" in capsys.readouterr().out
    _pair(phone)
    cli.main(["peer", "list"])
    out = capsys.readouterr().out
    assert "phone" in out and "p8a" in out and phone.url in out and "not paired" not in out


def test_not_paired_says_how_and_sends_nothing(phone, capsys):
    assert cli.main(["restart-services", "--host", "p8a", "--only", "canvas"]) == 1
    out = capsys.readouterr().out
    assert "not paired" in out and "media-visual-canvas pair --device" in out
    assert "media peer pair phone" in out and phone.seen == []


def test_host_posts_the_options_with_the_token_and_prints_the_lines(phone, capsys):
    _pair(phone)
    phone.answer = (200, {"ok": True, "host": "p8a", "rc": 0, "own": ["media-visual-canvas"],
                          "lines": ["media-visual-canvas answers this request: restarting "
                                    "in 10 s, detached", "scheduled: media-visual-canvas"]})
    assert cli.main(["restart-services", "--host", "p8a", "--only", "canvas"]) == 0
    path, headers, body = phone.seen[-1]
    assert path == "/services/restart"
    assert headers["Authorization"] == f"Bearer {phone.token}"
    assert headers["User-Agent"].startswith("agent-media-peer/")
    assert body == {"only": ["canvas"], "force": False, "dry_run": False}
    out = capsys.readouterr().out
    assert f"on p8a ({phone.url}):" in out and "restarting in 10 s, detached" in out


def test_after_force_and_dry_run_are_passed(phone, capsys):
    _pair(phone)
    phone.answer = (200, {"ok": False, "rc": 3, "busy": ["sessiond: session 1 is working"],
                          "lines": ["busy: sessiond: session 1 is working"]})
    assert cli.main(["restart-services", "--host", "phone", "--only", "sessiond",
                     "--after", "30", "--dry-run"]) == 3
    assert phone.seen[-1][2] == {"only": ["sessiond"], "after": 30.0, "force": False,
                                 "dry_run": True}
    assert "busy: sessiond: session 1 is working" in capsys.readouterr().out
    cli.main(["restart-services", "--host", "phone", "--force"])
    assert phone.seen[-1][2] == {"force": True, "dry_run": False}


@pytest.mark.parametrize("status,answer,rc,said", [
    (409, {"ok": False, "rc": 3, "lines": ["refused, busy: speech: a reply"]}, 3, "refused, busy"),
    (400, {"ok": False, "rc": 2, "lines": ["unknown service(s): nope"]}, 2, "unknown service"),
    (500, {"ok": False, "rc": 1, "lines": ["x: restart FAILED: boom"]}, 1, "restart FAILED"),
    (401, {"ok": False, "error": "unauthorized"}, 1, "refused this host's token"),
    (404, {"error": "not found"}, 1, "runs older code"),
])
def test_the_remote_answer_is_the_exit_code(phone, capsys, status, answer, rc, said):
    _pair(phone)
    phone.answer = (status, answer)
    assert cli.main(["restart-services", "--host", "phone"]) == rc
    assert said in capsys.readouterr().out


def test_unreachable_is_1(phone, capsys, monkeypatch):
    _pair(phone)
    rec = json.loads(peer_link._file("phone").read_text())
    rec["url"] = "http://127.0.0.1:9"
    peer_link._file("phone").write_text(json.dumps(rec))
    assert cli.main(["restart-services", "--host", "phone"]) == 1
    assert "could not reach" in capsys.readouterr().out


def test_round_trip_through_the_real_route(tmp_path, monkeypatch, capsys):
    """This CLI against the real server Handler (in-process, its services
    fake): pair with a code it minted, then restart its own canvas, which
    comes back scheduled 10 s out, detached, nothing restarted now."""
    pytest.importorskip("agent_media_server.app")
    from http.server import ThreadingHTTPServer

    from agent_media_server import app, devices

    for k in [k for k in os.environ if k.startswith("MEDIA_")]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MEDIA_CONFIG", str(tmp_path / "none.toml"))
    # Never this host's real canvas (run_device asks it first).
    monkeypatch.setenv("MEDIA_PHONE_JOBS_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("AMUX_AUTH_TOKEN", raising=False)
    devices._reset_for_tests()
    svcs = [rs.Service("media-visual-canvas", "up", "runit"),
            rs.Service("media-sessiond", "up", "runit")]
    done, scheduled = [], []
    monkeypatch.setattr(rs, "services", lambda: list(svcs))
    monkeypatch.setattr(rs, "_restart_one", lambda s: done.append(s.name) or (True, ""))
    monkeypatch.setattr(rs, "health", lambda d, within=None: [])
    monkeypatch.setattr(rs, "alert", lambda *a, **k: None)
    monkeypatch.setattr(rs, "sessiond_sessions", lambda: [])
    monkeypatch.setattr(rs, "_own_cgroup", lambda: "")
    monkeypatch.setattr(rs, "_main_pid",
                        lambda s: os.getpid() if s.name == "media-visual-canvas" else 0)
    monkeypatch.setattr(rs, "schedule", lambda sel, after, force: scheduled.append(
        ([s.name for s in sel], after)) or {"how": "detached", "unit": "pid 1", "log": "-"})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, args=(0.01,), daemon=True).start()
    try:
        code, _ = devices.mint_code("red5 media")
        base = f"http://127.0.0.1:{srv.server_port}"
        assert cli.main(["peer", "pair", "p8a", code, "--server", base]) == 0
        assert cli.main(["restart-services", "--host", "p8a", "--only", "canvas"]) == 0
        assert scheduled == [(["media-visual-canvas"], 10.0)] and done == []
        assert "restarting in 10 s, detached" in capsys.readouterr().out
        assert cli.main(["restart-services", "--host", "p8a", "--only", "sessiond",
                         "--dry-run"]) == 0
        assert done == [] and "busy checks: clear" in capsys.readouterr().out
    finally:
        srv.shutdown()
        srv.server_close()


def test_canvas_names_the_canvas_wherever_it_runs():
    svcs = [rs.Service("canvas-local", "up", "runit"),
            rs.Service("agent-media-visual-canvas", "up", "systemd"),
            rs.Service("phone-jobs", "up", "runit")]
    sel, unknown = rs.resolve(["canvas"], svcs)
    assert [s.name for s in sel] == ["canvas-local", "agent-media-visual-canvas"] and not unknown
    assert rs.resolve(["canvas-local"], svcs)[0] == [svcs[0]]


def test_no_server_here_to_ask_falls_through_to_the_peer(phone, capsys):
    """run_device finds no server of this host's own (a closed port): NAME is
    then a peer, as before."""
    assert rs.run_device("phone") is None


def test_json_mode_prints_runs_dict(monkeypatch, capsys):
    seen = {}

    def fake_run(only, **kw):
        seen.update(only=only, **{k: v for k, v in kw.items() if k != "echo"})
        kw["echo"]("canvas-local: restarted")
        return {"rc": 0, "lines": ["canvas-local: restarted"]}

    monkeypatch.setattr(rs, "run", fake_run)
    assert rs.main(["--json", "--only", "canvas", "--after", "5", "--dry-run"]) == 0
    out, err = capsys.readouterr()
    got = json.loads(out.strip().splitlines()[-1])
    assert got["rc"] == 0 and got["host"] == rs.host()
    assert "canvas-local: restarted" in err
    assert seen == {"only": ["canvas"], "after": 5.0, "force": False, "dry_run": True,
                    "caller": "", "detach_own": True}
