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
