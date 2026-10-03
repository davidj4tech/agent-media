"""Commands for the phone's Termux, carried by a worker that dials out (roadmap item 15).

The music target runs its phone-side work over `ssh p8a`: is a track
cached, fetch it (`play-local --fetch-only`: YouTube refuses red5's
data-centre address, so the download has to be the phone's), its title,
its chapters (`ffprobe`), a mix or a search (`yt-dlp --flat-playlist`). All
of it goes through `music_local.phone_argv`, the one place that builds the
`ssh` argv. ssh to the phone needs the tailnet.

So the phone dials out instead. A small Termux service
(`deploy/phone/service/phone-jobs`), paired as its own device, holds
`GET /jobs/events` open through whatever address the app uses (the
tunnel), runs each job with `sh -c 'cd "$HOME" && <cmd>'` as ssh did, and
answers `POST /jobs/result {id, rc, out, err}`. On red5, `phone_argv`
becomes `python -m agent_media_core.phone_run <cmd>`, which asks the canvas
(`POST /jobs/run`, the host's token, never on the public listener) and
prints what came back, with the same exit code. No caller changes.

What the worker may be asked is what ssh to the phone allowed already, so
the doors are kept as narrow: a job is queued only by the host's token on
the canvas's own (tailnet) listener, and handed only to the one device named
by `MEDIA_PHONE_JOBS_DEVICE`. stdin is not carried: the two callers that pipe
a file in (seeding the Termux player from red5) fail and fall back, as they
do when ssh does.

    run(cmd, timeout)       → {rc, out, err}; rc 255 with no worker / on timeout
    serve(h, device)        → the worker's stream (`job` frames)
    result(device, body)    → the worker's answer
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from collections import deque

#: Longest a caller may wait, and the default (a fetch is a download).
MAX_TIMEOUT_S = 900.0
DEFAULT_TIMEOUT_S = 600.0
#: A worker's answer, at most (out and err together).
MAX_RESULT = 1 << 20
#: A queued job no worker took within this is answered "no worker".
PICKUP_S = 10.0
PING_S = 30.0

_COND = threading.Condition()
_JOBS: dict[str, dict] = {}
_QUEUE: deque[str] = deque()
#: Connected worker streams, by token.
_WORKERS: dict[int, float] = {}
_NEXT = 0


def worker_device() -> str:
    return os.environ.get("MEDIA_PHONE_JOBS_DEVICE", "").strip()


def connected() -> bool:
    with _COND:
        return bool(_WORKERS)


def run(cmd: str, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Queue `cmd` for the worker and wait for its answer."""
    timeout = max(1.0, min(float(timeout or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
    if not cmd or not isinstance(cmd, str):
        return {"rc": 2, "out": "", "err": "no command"}
    with _COND:
        if not _WORKERS:
            return {"rc": 255, "out": "", "err": "no phone worker connected"}
        jid = secrets.token_hex(6)
        job = {"id": jid, "cmd": cmd, "timeout": timeout, "at": time.time(),
               "taken": None, "done": False}
        _JOBS[jid] = job
        _QUEUE.append(jid)
        _COND.notify_all()
        end = time.monotonic() + timeout + 5
        pickup = time.monotonic() + PICKUP_S
        while not job["done"]:
            now = time.monotonic()
            if now >= end:
                break
            if job["taken"] is None and now >= pickup:
                break
            _COND.wait(timeout=min(end, pickup if job["taken"] is None else end) - now)
        _JOBS.pop(jid, None)
        if jid in _QUEUE:
            _QUEUE.remove(jid)
        if job["done"]:
            return {"rc": job["rc"], "out": job["out"], "err": job["err"]}
        why = "no phone worker took it" if job["taken"] is None else "timed out on the phone"
        return {"rc": 255, "out": "", "err": why}


def result(device: str, body) -> tuple[bool, dict]:
    if not worker_device() or device != worker_device():
        return False, {"status": 403, "error": "not the phone's worker"}
    if not isinstance(body, dict):
        return False, {"status": 400, "error": "a JSON object, please"}
    with _COND:
        job = _JOBS.get(str(body.get("id") or ""))
        if job is None:
            return True, {"late": True}
        try:
            job["rc"] = int(body.get("rc", 255))
        except (TypeError, ValueError):
            job["rc"] = 255
        job["out"] = str(body.get("out") or "")[:MAX_RESULT]
        job["err"] = str(body.get("err") or "")[:MAX_RESULT]
        job["done"] = True
        _COND.notify_all()
    return True, {}


def serve(h, device: str) -> bool:
    """The worker's stream: each queued job once, as a `job` frame."""
    from .app import _json

    if not worker_device() or device != worker_device():
        _json(h, 403, {"ok": False, "error": "not the phone's worker"})
        return True
    global _NEXT
    with _COND:
        _NEXT += 1
        token = _NEXT
        _WORKERS[token] = time.monotonic()
    h.close_connection = True
    try:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-store")
        h.send_header("X-Accel-Buffering", "no")
        h.send_header("Connection", "close")
        h.end_headers()
        h.wfile.write(b"retry: 5000\n\n")
        h.wfile.flush()
        last = time.monotonic()
        while True:
            with _COND:
                _COND.wait_for(lambda: bool(_QUEUE),
                               timeout=max(0.01, PING_S - (time.monotonic() - last)))
                # No longer the named worker (the setting moved): take nothing
                # more, and let the stream go.
                if device != worker_device():
                    return True
                out = []
                while _QUEUE:
                    job = _JOBS.get(_QUEUE.popleft())
                    if job is not None:
                        job["taken"] = time.monotonic()
                        out.append({"id": job["id"], "cmd": job["cmd"],
                                    "timeout": job["timeout"]})
            for j in out:
                h.wfile.write(f"event: job\ndata: {json.dumps(j)}\n\n".encode())
                last = time.monotonic()
            if not out and time.monotonic() - last >= PING_S:
                h.wfile.write(b"event: ping\ndata: {}\n\n")
                last = time.monotonic()
            h.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        with _COND:
            _WORKERS.pop(token, None)
            _COND.notify_all()
    return True


def _reset_for_tests() -> None:
    with _COND:
        _JOBS.clear()
        _QUEUE.clear()
        _WORKERS.clear()
