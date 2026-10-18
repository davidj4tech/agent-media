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
    serve(h, device, kinds) → the worker's stream (`job` frames)
    result(device, body)    → the worker's answer
    restart(name, body)     → (status, answer): `media restart-services --host
                              <the phone>` (POST /jobs/restart)

**A restart is a job of its own kind** (David, 18 Oct 2026: "I don't want to
have to rely on tailscale"). `media restart-services --host NAME` used to
POST the other host's `/services/restart`, which needs that server to be
reachable — and the phone's canvas listens on loopback only. So when NAME is
the device this worker is, the restart goes down the worker's stream instead:
a `job` frame `{id, kind: "restart", restart: {only, after, force, dry_run},
timeout}`, which the worker turns into `restart_services.run()` on the phone
(never a shell command) and answers with that run's dict as `result`. Only
those four fields exist: any other key, or a value of the wrong shape, is
refused here (400) and again on the phone. A worker takes restart jobs only
when its stream said it can (`GET /jobs/events?kinds=restart`), so an older
worker is never handed one, and the caller hears at once that the phone
needs a pull. Queuing one is `POST /jobs/restart`, an app route (so on the
public listener too): the host's token, or a caller `auth.may_admin` lets
in, as for `POST /services/restart` — a narrower door than `/jobs/run`,
which stays the canvas's own.
"""

from __future__ import annotations

import json
import os
import re
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
#: Past a job's own timeout, how long its answer may still take to arrive.
ANSWER_MARGIN_S = 5.0
#: How long a restart's caller waits for the phone to say it was done,
#: scheduled or refused (a restart now waits out its 20 s health check).
RESTART_TIMEOUT_S = 60.0
#: How long an update's caller waits: a pull (at most 90 s on the device),
#: then its restart (up to 60 s), and a margin.
UPDATE_TIMEOUT_S = 180.0
#: The kinds a worker may say it takes besides a command (`?kinds=`).
KINDS = ("restart", "update")
#: A restart's fields, and nothing else.
RESTART_KEYS = frozenset({"only", "after", "force", "dry_run"})
#: A service name as restart_services matches it (unit stem, runit dir, a
#: short name, `am-snapfifo@am`).
SERVICE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,63}")

_COND = threading.Condition()
_JOBS: dict[str, dict] = {}
_QUEUE: deque[str] = deque()
#: Connected worker streams, by token: {"at", "kinds"}.
_WORKERS: dict[int, dict] = {}
_NEXT = 0


def worker_device() -> str:
    return os.environ.get("MEDIA_PHONE_JOBS_DEVICE", "").strip()


def connected() -> bool:
    with _COND:
        return bool(_WORKERS)


def _submit(job: dict, timeout: float) -> dict | None:
    """Queue `job` and wait for its answer; the job when answered, else None
    with `job["why"]` saying why. Called holding `_COND`. The job is gone from
    the queue and the table either way: nothing outlives its wait."""
    jid = job["id"]
    _JOBS[jid] = job
    _QUEUE.append(jid)
    _COND.notify_all()
    end = time.monotonic() + timeout + ANSWER_MARGIN_S
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
        return job
    job["why"] = "no phone worker took it" if job["taken"] is None else "timed out on the phone"
    return None


def _new(kind: str, timeout: float, **fields) -> dict:
    return {"id": secrets.token_hex(6), "kind": kind, "timeout": timeout,
            "at": time.time(), "taken": None, "done": False, **fields}


def run(cmd: str, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Queue `cmd` for the worker and wait for its answer."""
    timeout = max(1.0, min(float(timeout or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
    if not cmd or not isinstance(cmd, str):
        return {"rc": 2, "out": "", "err": "no command"}
    with _COND:
        if not _WORKERS:
            return {"rc": 255, "out": "", "err": "no phone worker connected"}
        job = _new("cmd", timeout, cmd=cmd)
        if _submit(job, timeout) is None:
            return {"rc": 255, "out": "", "err": job["why"]}
        return {"rc": job["rc"], "out": job["out"], "err": job["err"]}


# --- a restart, as its own kind of job ---------------------------------------------


def restart_spec(body) -> tuple[dict | None, str]:
    """`({only, after, force, dry_run}, "")` from a request body, or `(None,
    why)`. Exactly these fields: anything else is refused, not dropped, so a
    caller that thinks it can send more learns that it cannot."""
    if not isinstance(body, dict):
        return None, "a JSON object, please"
    extra = sorted(set(body) - RESTART_KEYS)
    if extra:
        return None, f"not a restart field: {', '.join(extra)}"
    only = body.get("only")
    if isinstance(only, str):
        only = only.split()
    if only is not None and not (isinstance(only, list) and len(only) <= 32 and all(
            isinstance(n, str) and SERVICE_RE.fullmatch(n) for n in only)):
        return None, "only: a list of service names"
    after = body.get("after", 0)
    if isinstance(after, bool) or not isinstance(after, (int, float)) \
            or not 0 <= after <= 24 * 3600:
        return None, "after: seconds, 0 to 86400"
    for k in ("force", "dry_run"):
        if body.get(k) not in (None, True, False):
            return None, f"{k}: true or false"
    return {"only": list(only) if only else None, "after": float(after),
            "force": body.get("force") is True, "dry_run": body.get("dry_run") is True}, ""


def device_for(name: str) -> dict | None:
    """The paired device NAME names, when it is this server's jobs worker:
    its id, its whole name, or the first word of its name, any case
    ("p8a" for "p8a Termux jobs"). None otherwise: NAME is then not a host
    this server can reach down a stream, and the caller tries it as a peer."""
    from . import devices

    wid = worker_device()
    n = (name or "").strip().casefold()
    if not wid or not n:
        return None
    for d in devices.list_devices():
        if d.get("id") != wid:
            continue
        full = str(d.get("name") or "").strip().casefold()
        if n in (wid.casefold(), full) or (full.split() and full.split()[0] == n):
            return d
    return None


def _fail(lines: list[str], **extra) -> dict:
    return {"ok": False, "rc": 1, "lines": lines, "selected": [], "busy": [],
            "unknown": [], "scheduled": None, "own": [], "after": 0, **extra}


def _device_job(kind: str, name: str, body, timeout: float) -> tuple[int, dict | None, dict]:
    """Queue a `kind` job ({only, after, force, dry_run}) for the device NAME
    names and wait for it: `(status, job, answer)`, `job` None unless it was
    answered (status 200), `answer` the error to send otherwise."""
    dev = device_for(name)
    if dev is None:
        return 404, None, {"ok": False, "code": "not_a_device",
                           "error": f"{name!r} is not this server's phone jobs worker"}
    spec, why = restart_spec(body)
    if spec is None:
        return 400, None, {"ok": False, "error": why}
    label = str(dev.get("name") or dev.get("id"))
    timeout = max(1.0, min(float(timeout), MAX_TIMEOUT_S))
    with _COND:
        if not _WORKERS:
            return 503, None, _fail([f"{label} is offline: its jobs worker is not "
                                     "connected to this server (phone-jobs on the phone)"],
                                    device=label)
        if not any(kind in w["kinds"] for w in _WORKERS.values()):
            return 503, None, _fail([f"{label}'s jobs worker is too old to take "
                                     f"{'a restart' if kind == 'restart' else 'an update'}: "
                                     "pull agent-media on the phone and restart its "
                                     "phone-jobs service"], device=label)
        job = _new(kind, timeout, **{kind: spec})
        if _submit(job, timeout) is None:
            return 503, None, _fail([f"{label}: {job['why']} (waited {timeout:g} s); "
                                     "nothing is left queued"], device=label)
    job["label"] = label
    return 200, job, {}


def restart(name: str, body, timeout: float = RESTART_TIMEOUT_S) -> tuple[int, dict]:
    """`media restart-services --host NAME` for the phone: `(status, answer)`.

    404 `code: not_a_device` when NAME is not this server's jobs worker (the
    caller then tries a peer); 400 a field that is not a restart's; 503 with
    `rc` 1 when the worker is not connected, too old to take a restart, did
    not take it or did not answer within `timeout`; else 200 and the phone's
    `run()` dict, `rc` its exit code, `device` the device's name."""
    status, job, err = _device_job("restart", name, body, timeout or RESTART_TIMEOUT_S)
    if job is None:
        return status, err
    label = job["label"]
    res = job.get("result") if isinstance(job.get("result"), dict) else {}
    if "rc" not in res:
        lines = [f"{label}: the worker could not run the restart (rc {job['rc']})"]
        if job.get("err"):
            lines += job["err"].strip().splitlines()[-5:]
        return 200, _fail(lines, device=label, rc=job["rc"] or 1)
    return 200, {**_fail([]), **res, "ok": int(res["rc"]) == 0, "device": label}


def update(name: str, body, timeout: float = UPDATE_TIMEOUT_S) -> tuple[int, dict]:
    """`media update --host NAME`: `git pull --ff-only` of the device's
    checkout, then, when HEAD moved, its restart_services.run() (or `only`'s),
    by default 10 s out, detached. `(status, answer)`, the statuses as
    `restart`'s; 200's answer is `{ok, rc, lines, pulled: {from, to}, moved,
    restart: run()'s dict or None, device}`. The device refuses a dirty
    checkout (rc 1, nothing pulled or restarted)."""
    status, job, err = _device_job("update", name, body, timeout or UPDATE_TIMEOUT_S)
    if job is None:
        return status, {**err, "pulled": None, "moved": False, "restart": None}
    label = job["label"]
    res = job.get("result") if isinstance(job.get("result"), dict) else None
    if res is None:
        lines = [f"{label}: the worker could not run the update (rc {job['rc']})"]
        if job.get("err"):
            lines += job["err"].strip().splitlines()[-5:]
        return 200, {**_fail(lines, device=label, rc=job["rc"] or 1),
                     "pulled": None, "moved": False, "restart": None}
    return 200, {"ok": job["rc"] == 0, "rc": job["rc"], "device": label, **res}


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
        if job["kind"] == "restart":
            job["result"] = _clean_result(body.get("result"), job["rc"])
        elif job["kind"] == "update":
            job["result"] = _clean_update(body.get("result"))
        job["done"] = True
        _COND.notify_all()
    return True, {}


def _clean_result(got, rc: int) -> dict | None:
    """A restart's `result` as the phone sent it, kept to run()'s shape."""
    if not isinstance(got, dict):
        return None
    out: dict = {"rc": rc}
    for k in ("lines", "selected", "busy", "unknown", "own"):
        v = got.get(k)
        out[k] = [str(x)[:2000] for x in v[:200]] if isinstance(v, list) else []
    sch = got.get("scheduled")
    out["scheduled"] = ({str(k): str(v)[:500] for k, v in list(sch.items())[:10]}
                        if isinstance(sch, dict) else None)
    after = got.get("after")
    out["after"] = after if isinstance(after, (int, float)) and not isinstance(after, bool) else 0
    if got.get("host"):
        out["host"] = str(got["host"])[:64]
    return out


def _clean_update(got) -> dict | None:
    """An update's `result` as the device sent it, kept to its shape."""
    if not isinstance(got, dict):
        return None
    pulled = got.get("pulled")
    pulled = ({"from": str(pulled.get("from") or "")[:40], "to": str(pulled.get("to") or "")[:40]}
              if isinstance(pulled, dict) else None)
    lines = got.get("lines")
    restart = got.get("restart")
    return {"pulled": pulled, "moved": got.get("moved") is True,
            "lines": [str(x)[:2000] for x in lines[:200]] if isinstance(lines, list) else [],
            "restart": (_clean_result(restart, int(restart.get("rc", 1))
                                      if isinstance(restart.get("rc"), int) else 1)
                        if isinstance(restart, dict) else None)}


def _frame(job: dict) -> dict:
    if job["kind"] in ("restart", "update"):
        return {"id": job["id"], "kind": job["kind"], job["kind"]: job[job["kind"]],
                "timeout": job["timeout"]}
    return {"id": job["id"], "cmd": job["cmd"], "timeout": job["timeout"]}


def serve(h, device: str, kinds=()) -> bool:
    """The worker's stream: each queued job once, as a `job` frame. A
    command goes to any worker; another kind only to a stream that named it
    in `kinds` (`?kinds=restart`)."""
    from .app import _json

    if not worker_device() or device != worker_device():
        _json(h, 403, {"ok": False, "error": "not the phone's worker"})
        return True
    global _NEXT
    with _COND:
        _NEXT += 1
        token = _NEXT
        _WORKERS[token] = {"at": time.monotonic(),
                           "kinds": {"cmd", *(k for k in kinds if k in KINDS)}}
        mine = _WORKERS[token]["kinds"]
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
                _COND.wait_for(lambda: any(_JOBS.get(j, {}).get("kind") in mine
                                           for j in _QUEUE),
                               timeout=max(0.01, PING_S - (time.monotonic() - last)))
                # No longer the named worker (the setting moved): take nothing
                # more, and let the stream go.
                if device != worker_device():
                    return True
                out = []
                for jid in list(_QUEUE):
                    job = _JOBS.get(jid)
                    if job is None:
                        _QUEUE.remove(jid)
                    elif job["kind"] in mine:
                        _QUEUE.remove(jid)
                        job["taken"] = time.monotonic()
                        out.append(_frame(job))
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
