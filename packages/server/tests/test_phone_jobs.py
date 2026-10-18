"""The phone's Termux worker, dialled out (phone_jobs.py, roadmap item 15).

End to end over real HTTP: the worker is the real script
(deploy/phone/service/phone-jobs/phone-jobs.py), run here against the
in-process canvas; the caller is the real `agent_media_core.phone_run`,
which is what `music_local.phone_argv` hands its callers.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_media_core import phone_run
from agent_media_server import phone_jobs

from test_contract import AUTH, call, server, shelf, signed_in, typed  # noqa: F401
from test_mic import _device
from test_thread_events import _wait

WORKER = Path(__file__).resolve().parents[3] / "deploy/phone/service/phone-jobs/phone-jobs.py"


@pytest.fixture(autouse=True)
def fresh():
    phone_jobs._reset_for_tests()
    yield
    phone_jobs._reset_for_tests()


@pytest.fixture()
def worker(server, monkeypatch, tmp_path):
    """The worker script, paired, streaming from the in-process server."""
    headers, dev = _device("p8a Termux jobs")
    monkeypatch.setenv("MEDIA_PHONE_JOBS_DEVICE", dev)
    monkeypatch.setenv("MEDIA_PHONE_JOBS_SERVER", "http://%s:%d" % server)
    monkeypatch.setenv("MEDIA_PHONE_JOBS_TOKEN", headers["Authorization"].split()[1])
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("phone_jobs_worker", WORKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    threading.Thread(target=lambda: _forever(mod), daemon=True).start()
    assert _wait(phone_jobs.connected)
    return headers


def _forever(mod):
    try:
        mod._stream(mod._token())
    except Exception:  # noqa: BLE001 — the test's server going away
        pass


@pytest.fixture()
def host(server, monkeypatch):
    monkeypatch.setenv("AMUX_AUTH_TOKEN", "hosttok")
    monkeypatch.setenv("MEDIA_PHONE_JOBS_URL", "http://%s:%d" % server)


def test_a_command_runs_on_the_phone_as_ssh_ran_it(worker, host, tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "play-local").write_text("#!/bin/sh\necho fetching \"$2\" >&2\necho /x/$2.mka\n")
    (tmp_path / "bin" / "play-local").chmod(0o755)
    # Relative to $HOME, as `ssh p8a bin/play-local` is.
    rc, out, err = phone_run.run("bin/play-local --fetch-only abc", timeout=10)
    assert (rc, out, err) == (0, "/x/abc.mka\n", "fetching abc\n")
    assert phone_run.run("exit 3", timeout=10)[0] == 3


def test_phone_argv_is_a_drop_in_for_ssh(worker, host, monkeypatch):
    from agent_media_core.sinks import music_local

    monkeypatch.setenv("MEDIA_PHONE_JOBS", "1")
    monkeypatch.setattr(music_local, "fetch_is_local", lambda: False)
    argv = music_local.phone_argv('echo "$HOME" | wc -c')
    assert argv[:3] == [sys.executable, "-m", "agent_media_core.phone_run"]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=os.environ)
    assert r.returncode == 0 and int(r.stdout.strip()) > 1


def test_no_worker_is_255_at_once(server, host, monkeypatch):
    monkeypatch.setenv("MEDIA_PHONE_JOBS_DEVICE", "d_nobody")
    t0 = time.monotonic()
    rc, _, err = phone_run.run("true", timeout=10)
    assert rc == 255 and "no phone worker" in err
    assert time.monotonic() - t0 < 2


def test_only_the_named_device_is_the_worker(server, host, monkeypatch):
    pixel, _ = _device("Pixel 8a")
    monkeypatch.setenv("MEDIA_PHONE_JOBS_DEVICE", "d_someone_else")
    assert call(server, "POST", "/jobs/result", {"id": "x", "rc": 0}, pixel)[0].status == 403
    from test_thread_events import Stream
    st = Stream(server, "/jobs/events", pixel)
    assert st.status == 403
    st.close()


def test_queueing_takes_the_host_token(server, monkeypatch):
    monkeypatch.setenv("AMUX_AUTH_TOKEN", "hosttok")
    r = call(server, "POST", "/jobs/run", {"cmd": "true"}, {"X-Auth-Token": "wrong"})
    assert r[0].status == 401


def test_the_public_listener_cannot_queue(monkeypatch):
    """`/jobs/run` is the canvas's own route, not the app's: the tunnel's
    listener (PublicHandler) answers it 404."""
    from http.server import ThreadingHTTPServer

    from agent_media_visual import canvas

    monkeypatch.setenv("AMUX_AUTH_TOKEN", "hosttok")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), canvas.PublicHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = call(srv.server_address, "POST", "/jobs/run", {"cmd": "true"},
                 {"X-Auth-Token": "hosttok"})
        assert r[0].status == 404
    finally:
        srv.shutdown()


# --- a restart, its own kind of job (David, 18 Oct 2026: "I don't want to
# have to rely on tailscale") -------------------------------------------------------
#
# `media restart-services --host p8a` on red5 is restart_services.run_device:
# this server's POST /jobs/restart, down the stream the phone's worker holds
# open. The worker runs agent_media_core.restart_services.run() through a
# fake "python" that fakes every service first: nothing real is restarted.

from agent_media_core import cli as core_cli  # noqa: E402
from agent_media_core import restart_services as rs  # noqa: E402

FAKE_PY = r'''#!{python}
import os, sys
from agent_media_core import restart_services as rs
log = os.environ["FAKE_RESTART_LOG"]
def note(line):
    with open(log, "a") as f:
        f.write(line + "\n")
rs.services = lambda: [rs.Service("canvas-local", "up", "runit"),
                       rs.Service("phone-jobs", "up", "runit"),
                       rs.Service("sink-speech", "up", "runit")]
rs._restart_one = lambda s: (note("restart " + s.name), (True, ""))[1]
rs.health = lambda done, within=None: []
rs.alert = lambda *a, **k: None
rs.busy = lambda sel, caller, now=False: ["a reply is being spoken"] if os.environ.get("FAKE_BUSY") else []
rs._own_cgroup = lambda: ""
# The worker is phone-jobs' main process: this script's parent.
rs._main_pid = lambda s: os.getppid() if s.name == "phone-jobs" else 0
rs.schedule = lambda sel, after, force: (note("schedule %s %s" % (" ".join(x.name for x in sel), after)),
                                         {{"how": "detached", "unit": "pid 1", "log": "-"}})[1]
assert sys.argv[1:3] == ["-m", "agent_media_core.restart_services"], sys.argv
note("argv " + " ".join(sys.argv[3:]))
sys.exit(rs.main(sys.argv[3:]))
'''


@pytest.fixture()
def fake_py(tmp_path, monkeypatch):
    py = tmp_path / "fake-python"
    py.write_text(FAKE_PY.format(python=sys.executable))
    py.chmod(0o755)
    log = tmp_path / "restart.log"
    log.touch()
    monkeypatch.setenv("MEDIA_PHONE_JOBS_PYTHON", str(py))
    monkeypatch.setenv("FAKE_RESTART_LOG", str(log))
    monkeypatch.delenv("FAKE_BUSY", raising=False)
    return lambda: log.read_text().splitlines()


def _worker_module():
    spec = importlib.util.spec_from_file_location("phone_jobs_worker_t", WORKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _empty() -> bool:
    with phone_jobs._COND:
        return not phone_jobs._JOBS and not phone_jobs._QUEUE


def test_a_restart_goes_down_the_workers_own_connection(worker, host, fake_py):
    said: list[str] = []
    res = rs.run_device("p8a", ["canvas"], echo=said.append)
    assert res["rc"] == 0, res
    assert "argv --json --only canvas" in fake_py()
    assert "restart canvas-local" in fake_py()          # `canvas` is the phone's canvas-local
    assert said[0] == "on p8a Termux jobs (down its own connection to this server):"
    assert "  canvas-local: restarted" in said
    assert res["device"] == "p8a Termux jobs" and res["selected"] == ["canvas-local"]
    assert _empty()


def test_the_cli_takes_the_device_channel_and_its_rc(worker, host, fake_py, capsys):
    # The worker restarting itself: run() sees it is inside phone-jobs and
    # schedules it detached, 10 s out, so the answer gets back first.
    assert core_cli.main(["restart-services", "--host", "p8a", "--only", "phone-jobs"]) == 0
    assert "schedule phone-jobs 10.0" in fake_py()
    assert not any(line.startswith("restart ") for line in fake_py())
    out = capsys.readouterr().out
    assert "on p8a Termux jobs" in out and "restarting in 10 s, detached" in out


def test_after_force_and_dry_run_reach_the_phone(worker, host, fake_py):
    res = rs.run_device("P8A", ["sink-speech"], after=30, force=True, dry_run=True)
    assert res["rc"] == 0
    assert "argv --json --only sink-speech --after 30.0 --force --dry-run" in fake_py()
    assert not any(line.startswith(("restart ", "schedule ")) for line in fake_py())


def test_busy_on_the_phone_is_its_exit_code(worker, host, fake_py, monkeypatch):
    monkeypatch.setenv("FAKE_BUSY", "1")
    res = rs.run_device("p8a", ["sink-speech"])
    assert res["rc"] == rs.BUSY and res["busy"] == ["a reply is being spoken"]
    assert not any(line.startswith("restart ") for line in fake_py())


def test_offline_is_1_at_once_and_nothing_stays_queued(server, host, monkeypatch, capsys):
    _, dev = _device("p8a Termux jobs")
    monkeypatch.setenv("MEDIA_PHONE_JOBS_DEVICE", dev)
    t0 = time.monotonic()
    assert core_cli.main(["restart-services", "--host", "p8a", "--only", "canvas"]) == 1
    assert time.monotonic() - t0 < 3
    assert "p8a Termux jobs is offline" in capsys.readouterr().out
    assert _empty()


def test_a_worker_too_old_for_restarts_is_never_handed_one(server, host, monkeypatch):
    from test_thread_events import Stream

    headers, dev = _device("p8a Termux jobs")
    monkeypatch.setenv("MEDIA_PHONE_JOBS_DEVICE", dev)
    st = Stream(server, "/jobs/events", headers)      # no ?kinds=restart
    try:
        assert _wait(phone_jobs.connected)
        status, ans = phone_jobs.restart("p8a", {"only": ["canvas"]})
        assert status == 503 and "too old" in ans["lines"][0] and ans["rc"] == 1
        assert _empty()
    finally:
        st.close()


def test_not_a_device_is_404_and_the_cli_tries_a_peer(server, host, monkeypatch):
    _, dev = _device("p8a Termux jobs")
    _device("homer media")                            # paired, but not the worker
    monkeypatch.setenv("MEDIA_PHONE_JOBS_DEVICE", dev)
    r = call(server, "POST", "/jobs/restart", {"device": "homer"}, {"X-Auth-Token": "hosttok"})
    assert r[0].status == 404 and r[1]["code"] == "not_a_device"
    assert rs.run_device("homer") is None
    tried = []
    monkeypatch.setattr(rs, "run_remote", lambda name, *a, **k: tried.append(name) or {"rc": 0})
    assert core_cli.main(["restart-services", "--host", "homer"]) == 0 and tried == ["homer"]


@pytest.mark.parametrize("body, why", [
    ({"only": ["canvas"], "cmd": "rm -rf ~"}, "not a restart field: cmd"),
    ({"only": ["canvas; reboot"]}, "only:"),
    ({"only": "canvas", "after": "soon"}, "after:"),
    ({"after": True}, "after:"),
    ({"force": "yes"}, "force:"),
])
def test_only_a_restarts_fields_are_queued(server, host, monkeypatch, body, why):
    _, dev = _device("p8a Termux jobs")
    monkeypatch.setenv("MEDIA_PHONE_JOBS_DEVICE", dev)
    phone_jobs._WORKERS[1] = {"at": 0, "device": dev, "kinds": {"cmd", "restart"}}
    r = call(server, "POST", "/jobs/restart", {"device": "p8a", **body},
             {"X-Auth-Token": "hosttok"})
    assert r[0].status == 400 and why in r[1]["error"]
    assert _empty()


def test_the_worker_refuses_anything_but_a_restarts_fields(monkeypatch):
    mod = _worker_module()
    assert mod.restart_argv({"only": ["canvas"]}, "py")[0] == [
        "py", "-m", "agent_media_core.restart_services", "--json", "--only", "canvas"]
    for spec in ({"cmd": "id"}, {"only": ["$(id)"]}, {"only": "canvas"},
                 {"after": -1}, {"dry_run": "1"}, "canvas"):
        assert mod.restart_argv(spec, "py")[0] is None, spec
    answered, ran = [], []
    monkeypatch.setattr(mod, "_answer", lambda jid, body, tok: answered.append((jid, body)))
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: ran.append(a))
    mod._handle({"id": "j1", "kind": "restart", "restart": {"only": ["x"], "cmd": "id"}}, "t")
    mod._handle({"id": "j2", "kind": "shell", "cmd": "id"}, "t")
    assert ran == []
    assert [(j, b["rc"]) for j, b in answered] == [("j1", 2), ("j2", 2)]
    assert "not a restart field: cmd" in answered[0][1]["err"]


def test_a_job_nobody_takes_is_given_up_after_pickup(server, host, monkeypatch):
    _, dev = _device("p8a Termux jobs")
    monkeypatch.setenv("MEDIA_PHONE_JOBS_DEVICE", dev)
    monkeypatch.setattr(phone_jobs, "PICKUP_S", 0.2)
    phone_jobs._WORKERS[1] = {"at": 0, "device": dev, "kinds": {"cmd", "restart"}}   # a stream gone quiet
    status, ans = phone_jobs.restart("p8a", {"only": ["canvas"]}, timeout=5)
    assert status == 503 and "no phone worker took it" in ans["lines"][0]
    assert _empty()


def test_a_job_taken_but_not_answered_times_out_and_a_late_answer_is_dropped(
        server, host, monkeypatch):
    _, dev = _device("p8a Termux jobs")
    monkeypatch.setenv("MEDIA_PHONE_JOBS_DEVICE", dev)
    monkeypatch.setattr(phone_jobs, "ANSWER_MARGIN_S", 0.0)
    phone_jobs._WORKERS[1] = {"at": 0, "device": dev, "kinds": {"cmd", "restart"}}
    taken = []

    def take():
        while True:
            with phone_jobs._COND:
                if phone_jobs._QUEUE:
                    jid = phone_jobs._QUEUE.popleft()
                    phone_jobs._JOBS[jid]["taken"] = time.monotonic()
                    taken.append(jid)
                    return
            time.sleep(0.01)

    threading.Thread(target=take, daemon=True).start()
    t0 = time.monotonic()
    status, ans = phone_jobs.restart("p8a", {"only": ["canvas"]}, timeout=1)
    assert status == 503 and "timed out on the phone" in ans["lines"][0]
    assert time.monotonic() - t0 < 3 and _empty()
    assert phone_jobs.result(dev, {"id": taken[0], "rc": 0, "result": {"rc": 0}}) == (
        True, {"late": True})


def test_queueing_a_restart_takes_the_host_token_or_an_admin(server, host, monkeypatch):
    _, dev = _device("p8a Termux jobs")
    monkeypatch.setenv("MEDIA_PHONE_JOBS_DEVICE", dev)
    r = call(server, "POST", "/jobs/restart", {"device": "p8a"}, {"X-Auth-Token": "wrong"})
    assert r[0].status == 401
    admin, _ = _device("David's laptop")              # may_admin: a paired device
    r = call(server, "POST", "/jobs/restart", {"device": "p8a"}, admin)
    assert r[0].status == 503 and r[1]["rc"] == 1     # past the gate: the phone is offline


# --- an update: pull, then restart, down the same connection (David, 18 Oct
# 2026) --------------------------------------------------------------------------
#
# `media update --host p8a` (restart_services.update_device): POST
# /jobs/update, a job of kind "update" down the worker's stream. The worker
# pulls a real git checkout here (a clone of a bare origin in tmp), then runs
# the same fake python as a restart.

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"}


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       env={**os.environ, **GIT_ENV})
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture()
def repos(tmp_path, monkeypatch):
    """(device checkout, upstream work tree): the device is a clone of origin."""
    origin, work, dev = tmp_path / "origin.git", tmp_path / "work", tmp_path / "device"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    (work / "a").write_text("1\n")
    _git(work, "add", "a")
    _git(work, "commit", "-q", "-m", "one")
    _git(work, "push", "-q", "origin", "HEAD:main")
    _git(tmp_path, "clone", "-q", str(origin), str(dev))
    monkeypatch.setenv("MEDIA_JOBS_CHECKOUT", str(dev))
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)
    return dev, work


def _upstream_commit(work):
    (work / "a").write_text("2\n")
    _git(work, "commit", "-q", "-am", "two")
    _git(work, "push", "-q", "origin", "HEAD:main")
    return _git(work, "rev-parse", "--short", "HEAD")


def test_an_update_pulls_then_restarts_down_the_workers_connection(worker, host, fake_py,
                                                                    repos):
    dev, work = repos
    before = _git(dev, "rev-parse", "--short", "HEAD")
    after = _upstream_commit(work)
    said: list[str] = []
    res = rs.update_device("p8a", echo=said.append)
    assert res["rc"] == 0, res
    assert res["pulled"] == {"from": before, "to": after} and res["moved"] is True
    assert _git(dev, "rev-parse", "--short", "HEAD") == after
    # The services the checkout owns, 10 s out, detached: the answer first.
    assert "argv --json --after 10.0" in fake_py()
    assert any(line.startswith("schedule canvas-local phone-jobs sink-speech 10.0")
               for line in fake_py())
    assert said[0] == "on p8a Termux jobs (down its own connection to this server):"
    assert f"  pulled {before} -> {after}" in said
    assert res["restart"]["scheduled"]["how"] == "detached"
    assert _empty()


def test_an_update_with_nothing_new_restarts_nothing(worker, host, fake_py, repos):
    res = rs.update_device("p8a", ["canvas"])
    assert res["rc"] == 0 and res["moved"] is False and res["restart"] is None
    assert res["lines"][-1] == "nothing to restart"
    assert not any(line.startswith("argv") for line in fake_py())


def test_a_dirty_checkout_is_never_pulled(worker, host, fake_py, repos):
    dev, work = repos
    before = _git(dev, "rev-parse", "--short", "HEAD")
    _upstream_commit(work)
    (dev / "a").write_text("local edit\n")
    res = rs.update_device("p8a")
    assert res["rc"] == 1 and res["pulled"] is None
    assert "uncommitted changes" in res["lines"][0]
    assert _git(dev, "rev-parse", "--short", "HEAD") == before
    assert (dev / "a").read_text() == "local edit\n"
    assert fake_py() == []


def test_a_dry_run_update_fetches_and_says(worker, host, fake_py, repos):
    dev, work = repos
    before = _git(dev, "rev-parse", "--short", "HEAD")
    after = _upstream_commit(work)
    res = rs.update_device("p8a", ["sink-speech"], dry_run=True)
    assert res["rc"] == 0, res
    assert res["lines"][0] == f"would pull {before} -> {after} (1 commit(s))"
    assert _git(dev, "rev-parse", "--short", "HEAD") == before      # not merged
    assert "argv --json --only sink-speech --dry-run --after 10.0" in fake_py()
    assert not any(line.startswith(("restart ", "schedule ")) for line in fake_py())


def test_the_cli_update_and_its_exit_code(worker, host, fake_py, repos, capsys):
    _upstream_commit(repos[1])
    assert core_cli.main(["update", "--host", "p8a", "--only", "canvas", "--after", "30"]) == 0
    assert "argv --json --only canvas --after 30.0" in fake_py()
    assert "pulled" in capsys.readouterr().out


def test_update_not_a_device_is_1_with_how(server, host, monkeypatch, capsys):
    _, dev = _device("p8a Termux jobs")
    monkeypatch.setenv("MEDIA_PHONE_JOBS_DEVICE", dev)
    assert rs.update_device("homer") is None
    assert core_cli.main(["update", "--host", "homer"]) == 1
    assert "not a jobs device" in capsys.readouterr().err


def test_a_worker_too_old_for_updates_is_never_handed_one(server, host, monkeypatch):
    _, dev = _device("p8a Termux jobs")
    monkeypatch.setenv("MEDIA_PHONE_JOBS_DEVICE", dev)
    phone_jobs._WORKERS[1] = {"at": 0, "device": dev, "kinds": {"cmd", "restart"}}  # 18 Oct's worker
    status, ans = phone_jobs.update("p8a", {})
    assert status == 503 and "too old to take an update" in ans["lines"][0]
    assert _empty()


def test_only_an_updates_fields_are_queued(server, host, monkeypatch):
    _, dev = _device("p8a Termux jobs")
    monkeypatch.setenv("MEDIA_PHONE_JOBS_DEVICE", dev)
    phone_jobs._WORKERS[1] = {"at": 0, "device": dev, "kinds": {"cmd", "restart", "update"}}
    r = call(server, "POST", "/jobs/update", {"device": "p8a", "ref": "evil"},
             {"X-Auth-Token": "hosttok"})
    assert r[0].status == 400 and "ref" in r[1]["error"]
    r = call(server, "POST", "/jobs/update", {"device": "p8a"}, {"X-Auth-Token": "wrong"})
    assert r[0].status == 401
    assert _empty()


def test_the_worker_refuses_an_update_with_another_field(monkeypatch):
    mod = _worker_module()
    answered, ran = [], []
    monkeypatch.setattr(mod, "_answer", lambda jid, body, tok: answered.append((jid, body)))
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: ran.append(a))
    mod._handle({"id": "u1", "kind": "update", "update": {"branch": "evil"}}, "t")
    mod._handle({"id": "u2", "kind": "update", "update": "main"}, "t")
    assert ran == [] and [b["rc"] for _, b in answered] == [2, 2]
    assert "update: not a restart field: branch" in answered[0][1]["err"]


def test_doctor_fix_uses_the_device_channel_for_a_device(worker, host, fake_py, repos):
    after = _upstream_commit(repos[1])
    ok, lines = core_cli._fix_host("p8a")
    assert ok, lines
    assert lines[0] == "via its own connection to this server (no ssh)"
    assert any(ln.startswith("fix=agent-media pulled ") and ln.endswith(after) for ln in lines)
    assert any(ln.startswith("restart=scheduled:") for ln in lines)


def test_doctor_fix_falls_back_to_ssh_for_any_other_host(server, host, monkeypatch):
    _, dev = _device("p8a Termux jobs")
    monkeypatch.setenv("MEDIA_PHONE_JOBS_DEVICE", dev)
    ran = []

    class R:
        returncode, stdout, stderr = 0, "fix=agent-media already at abc\n", ""

    monkeypatch.setattr(subprocess, "run", lambda argv, **k: ran.append(argv) or R())
    ok, lines = core_cli._fix_host("homer")
    assert ok and ran[0][:1] == ["ssh"] and lines == ["fix=agent-media already at abc"]


# --- any host as a jobs worker, several at once (David, 18 Oct 2026) ------------
#
# The laptop (pn) and the speaker (sp4) are jobs-only devices: paired with
# `pair --device "pn jobs" --jobs`, each holding its own stream with the same
# worker script in its generic mode. A restart or update goes to the device it
# names and no other; a command only ever to the phone; a jobs-only token opens
# no other route.

from agent_media_server import devices  # noqa: E402


def _jobs_device(name: str) -> "tuple[dict, str, str]":
    """A jobs-only device: (headers, device id, token)."""
    code, _ = devices.mint_code(name, jobs=True)
    got = devices.redeem(code, name)
    assert got["jobs"] is True
    return {"Authorization": f"Bearer {got['token']}"}, got["device_id"], got["token"]


def _generic_worker(server, tmp_path, token, monkeypatch):
    """The worker script in its generic mode, paired by jobs-worker.json."""
    mod = _worker_module()
    cfg = tmp_path / "jobs-worker.json"
    cfg.write_text('{"server": "http://%s:%d", "token": "%s"}' % (*server, token))
    monkeypatch.setattr(mod, "CONFIG_FILE", cfg)
    mod.GENERIC = True
    threading.Thread(target=lambda: _forever(mod), daemon=True).start()
    return mod


@pytest.fixture()
def pn(server, worker, monkeypatch, tmp_path):
    """The phone's worker (the `worker` fixture) and pn's, both connected."""
    headers, dev, token = _jobs_device("pn jobs")
    _generic_worker(server, tmp_path, token, monkeypatch)
    assert _wait(lambda: phone_jobs.connected(dev))
    return headers, dev


def test_a_restart_goes_to_the_device_it_names_and_no_other(pn, host, fake_py):
    res = rs.run_device("pn", ["canvas"])
    assert res["rc"] == 0 and res["device"] == "pn jobs", res
    assert fake_py().count("argv --json --only canvas") == 1
    res = rs.run_device("p8a", ["sink-speech"])
    assert res["device"] == "p8a Termux jobs"
    assert fake_py().count("argv --json --only sink-speech") == 1
    assert _empty()


def test_a_jobs_only_device_never_gets_a_command(server, host, monkeypatch, tmp_path):
    _, dev, token = _jobs_device("pn jobs")
    monkeypatch.setenv("MEDIA_PHONE_JOBS_DEVICE", "d_phone_not_here")
    _generic_worker(server, tmp_path, token, monkeypatch)
    assert _wait(lambda: phone_jobs.connected(dev))
    rc, _, err = phone_run.run("id", timeout=5)
    assert rc == 255 and "no phone worker" in err
    with phone_jobs._COND:
        kinds = [w["kinds"] for w in phone_jobs._WORKERS.values()]
    assert kinds == [{"restart", "update"}]


def test_a_jobs_only_token_opens_no_other_route(server, host, monkeypatch):
    headers, _, _ = _jobs_device("pn jobs")
    for method, path, body in (("POST", "/jobs/restart", {"device": "pn"}),
                               ("POST", "/services/restart", {"dry_run": True}),
                               ("GET", "/jobs/devices", None)):
        r = call(server, method, path, body, headers)
        assert r[0].status in (401, 403, 502, 503) and r[1].get("rc") is None, (path, r)


def test_a_worker_cannot_answer_another_devices_job(server, host, monkeypatch):
    _, phone = _device("p8a Termux jobs")
    _, pn_dev, _ = _jobs_device("pn jobs")
    monkeypatch.setenv("MEDIA_PHONE_JOBS_DEVICE", phone)
    job = phone_jobs._new("restart", 5, device=phone, restart={})
    phone_jobs._JOBS[job["id"]] = job
    ok, detail = phone_jobs.result(pn_dev, {"id": job["id"], "rc": 0})
    assert not ok and detail["status"] == 403 and not job["done"]
    ok, _ = phone_jobs.result("d_stranger", {"id": job["id"], "rc": 0})
    assert not ok


def test_jobs_devices_lists_every_worker(pn, host, server, capsys):
    r = call(server, "GET", "/jobs/devices", None, {"X-Auth-Token": "hosttok"})
    assert r[0].status == 200
    by = {d["name"]: d for d in r[1]["devices"]}
    assert by["pn jobs"]["connected"] and by["pn jobs"]["takes"] == ["restart", "update"]
    assert by["p8a Termux jobs"]["phone"] and "cmd" in by["p8a Termux jobs"]["takes"]
    assert core_cli.main(["jobs", "list"]) == 0
    out = capsys.readouterr().out
    assert "pn jobs" in out and "connected" in out and "the phone" in out


def test_revoking_a_jobs_device_ends_its_stream(server, monkeypatch, tmp_path):
    monkeypatch.setattr(phone_jobs, "PING_S", 0.2)
    _, dev, token = _jobs_device("pn jobs")
    _generic_worker(server, tmp_path, token, monkeypatch)
    assert _wait(lambda: phone_jobs.connected(dev))
    devices.set_jobs(dev, False)
    assert dev not in phone_jobs.jobs_devices()
    assert _wait(lambda: not phone_jobs.connected(dev))


def test_media_jobs_pair_trades_a_jobs_code(server, monkeypatch, tmp_path):
    from agent_media_core import jobs_worker

    monkeypatch.setattr(jobs_worker, "config_path", lambda: tmp_path / "jw.json")
    code, _ = devices.mint_code("pn jobs", jobs=True)
    link = "sasonica://pair?server=http%%3A%%2F%%2F%s%%3A%d&code=%s" % (*server, code)
    rec = jobs_worker.pair(link)
    assert rec["name"] == "pn jobs" and "warning" not in rec
    assert (tmp_path / "jw.json").stat().st_mode & 0o777 == 0o600
    assert jobs_worker.config()["server"] == "http://%s:%d" % server
    row = next(d for d in devices.list_devices() if d["id"] == rec["device_id"])
    assert devices.is_jobs_worker(row)
    # A code minted without --jobs is a full device: said, so it can be fixed.
    code, _ = devices.mint_code("sp4 jobs")
    rec = jobs_worker.pair(code, server="http://%s:%d" % server)
    assert "media-visual-canvas devices --jobs" in rec["warning"]


def test_the_devices_cli_sets_the_jobs_bit(capsys):
    _, dev = _device("sp4 jobs")
    assert devices.cli_devices(["--jobs", dev]) == 0
    assert devices.cli_devices([]) == 0
    assert "[jobs worker]" in capsys.readouterr().out
    assert devices.cli_devices(["--no-jobs", dev]) == 0
    assert dev not in phone_jobs.jobs_devices()
