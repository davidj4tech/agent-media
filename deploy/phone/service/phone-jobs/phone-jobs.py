#!/data/data/com.termux/files/usr/bin/env python3
"""The phone's Termux, taking red5's jobs over a connection it opens itself.

red5 used to run its phone-side music work as `ssh p8a <cmd>`: is a track
cached, fetch it (YouTube refuses red5's data-centre address, so the
download has to be this phone's), its title, its chapters, a mix or a
search. ssh needs the tailnet. This holds `GET /jobs/events` open on red5
instead (through the tunnel), runs each job the way ssh ran it —
`sh -c 'cd "$HOME" && <cmd>'` — and posts `{id, rc, out, err}` to
`/jobs/result`. agent-media server phone_jobs.py is the other side.

Pairing, once: on red5 `media-visual-canvas pair --device "p8a Termux jobs"
--server https://red5.sasonica.com`, then here `phone-jobs.py --pair <code>`,
which keeps the token in ~/.config/agent-media/phone-jobs.token. red5 then
needs MEDIA_PHONE_JOBS_DEVICE=<its device id>, printed here.

**Restarts** (David, 18 Oct 2026: "I don't want to have to rely on
tailscale"). `media restart-services --host p8a` on red5 sends a job of kind
`restart` down the same stream: `{id, kind: "restart", restart: {only, after,
force, dry_run}}`. It is not a command: this checks the four fields again
(anything else is refused, rc 2, nothing run), runs agent-media's own
`restart_services.run()` here (`python -m agent_media_core.restart_services
--json`, with the checkout's `.venv/bin/python`, else
MEDIA_PHONE_JOBS_PYTHON), and answers its dict as `result`. A restart of
this very service runs detached, so the answer gets out first. The stream
says it takes them (`?kinds=restart`); a job of any kind this does not know
is answered rc 2 and never run.

Env: MEDIA_PHONE_JOBS_SERVER (https://red5.sasonica.com),
MEDIA_PHONE_JOBS_TOKEN (else the token file), MEDIA_PHONE_JOBS_PYTHON (the
python with agent_media_core, else the checkout's venv).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

TOKEN_FILE = Path.home() / ".config" / "agent-media" / "phone-jobs.token"
#: The server pings every 30 s; silence for this long is a dead stream.
READ_TIMEOUT_S = 90
MAX_OUT = 1 << 20
#: Cloudflare turns away Python's own "Python-urllib/x.y" (error 1010), so
#: the worker names itself.
UA = "sasonica-phone-jobs/1"
#: The kinds of job besides a command this worker takes (`?kinds=`).
KINDS = ("restart",)
#: A restart's fields, and nothing else (server phone_jobs.RESTART_KEYS).
RESTART_KEYS = frozenset({"only", "after", "force", "dry_run"})
SERVICE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,63}")


def _server() -> str:
    return (os.environ.get("MEDIA_PHONE_JOBS_SERVER") or "https://red5.sasonica.com").rstrip("/")


def _token() -> str:
    tok = os.environ.get("MEDIA_PHONE_JOBS_TOKEN", "").strip()
    if tok:
        return tok
    try:
        return TOKEN_FILE.read_text().strip()
    except OSError:
        return ""


def _post(path: str, body: dict, token: str = "") -> dict:
    headers = {"Content-Type": "application/json", "User-Agent": UA}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(_server() + path, data=json.dumps(body).encode(),
                                 method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def _answer(job_id, body: dict, token: str) -> None:
    for attempt in range(3):
        try:
            _post("/jobs/result", {"id": job_id, **body}, token)
            return
        except (OSError, ValueError) as e:
            print(f"job {job_id}: result not delivered ({e})", flush=True)
            time.sleep(2 * (attempt + 1))


def _run(job: dict, token: str) -> None:
    cmd = str(job.get("cmd") or "")
    try:
        timeout = float(job.get("timeout") or 600)
    except (TypeError, ValueError):
        timeout = 600.0
    t0 = time.monotonic()
    try:
        r = subprocess.run(["sh", "-c", 'cd "$HOME" && ' + cmd], capture_output=True,
                           text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        rc, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        rc, out, err = 124, (e.stdout or "") if isinstance(e.stdout, str) else "", "timed out"
    except OSError as e:
        rc, out, err = 127, "", str(e)
    print(f"job {job.get('id')}: rc {rc} in {time.monotonic() - t0:.1f}s: {cmd[:80]}",
          flush=True)
    _answer(job.get("id"), {"rc": rc, "out": out[:MAX_OUT], "err": err[:MAX_OUT]}, token)


def restart_argv(spec, python: str = "") -> "tuple[list[str] | None, str]":
    """The argv that runs restart_services.run() for `spec`, or `(None, why)`.
    Only the four fields, each of its own shape: nothing from the job reaches
    a shell, and a service name is a name."""
    if not isinstance(spec, dict):
        return None, "restart: not an object"
    extra = sorted(set(spec) - RESTART_KEYS)
    if extra:
        return None, f"restart: not a restart field: {', '.join(extra)}"
    only = spec.get("only")
    if only is not None and not (isinstance(only, list) and len(only) <= 32 and all(
            isinstance(n, str) and SERVICE_RE.fullmatch(n) for n in only)):
        return None, "restart: only must be a list of service names"
    after = spec.get("after") or 0
    if isinstance(after, bool) or not isinstance(after, (int, float)) \
            or not 0 <= after <= 24 * 3600:
        return None, "restart: after must be seconds, 0 to 86400"
    for k in ("force", "dry_run"):
        if spec.get(k) not in (None, True, False):
            return None, f"restart: {k} must be true or false"
    argv = [python or _python(), "-m", "agent_media_core.restart_services", "--json"]
    if only:
        argv += ["--only", *only]
    if after:
        argv += ["--after", str(float(after))]
    if spec.get("force") is True:
        argv.append("--force")
    if spec.get("dry_run") is True:
        argv.append("--dry-run")
    return argv, ""


def _python() -> str:
    """The python that has agent_media_core: MEDIA_PHONE_JOBS_PYTHON, else the
    venv of the checkout this script lives in (the service dir links into
    it), else ~/projects/agent-media's (a copied service dir), else this
    one."""
    env = os.environ.get("MEDIA_PHONE_JOBS_PYTHON", "").strip()
    if env:
        return env
    for venv in (Path(__file__).resolve().parents[3] / ".venv" / "bin" / "python",
                 Path.home() / "projects" / "agent-media" / ".venv" / "bin" / "python"):
        if venv.exists():
            return str(venv)
    return sys.executable


def _restart(job: dict, token: str) -> None:
    """A restart job: restart_services.run() here, its dict as `result`."""
    argv, why = restart_argv(job.get("restart"))
    if argv is None:
        print(f"job {job.get('id')}: refused: {why}", flush=True)
        _answer(job.get("id"), {"rc": 2, "out": "", "err": why}, token)
        return
    try:
        timeout = max(5.0, float(job.get("timeout") or 60) - 5)
    except (TypeError, ValueError):
        timeout = 55.0
    t0 = time.monotonic()
    result = None
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, cwd=str(Path.home()))
        rc, err = r.returncode, r.stderr
        last = (r.stdout or "").strip().splitlines()[-1:] or [""]
        try:
            result = json.loads(last[0])
        except ValueError:
            result = None
        if not isinstance(result, dict):
            result = None
    except subprocess.TimeoutExpired:
        rc, err = 124, f"restart_services did not answer within {timeout:.0f} s"
    except OSError as e:
        rc, err = 127, f"{argv[0]}: {e}"
    print(f"job {job.get('id')}: restart {' '.join(argv[4:]) or 'all'}: rc {rc} "
          f"in {time.monotonic() - t0:.1f}s", flush=True)
    body = {"rc": rc, "out": "", "err": (err or "")[-MAX_OUT:]}
    if result is not None:
        body["result"] = result
    _answer(job.get("id"), body, token)


def _handle(job: dict, token: str) -> None:
    kind = job.get("kind") or "cmd"
    if kind == "cmd":
        _run(job, token)
    elif kind == "restart":
        _restart(job, token)
    else:
        _answer(job.get("id"), {"rc": 2, "out": "", "err": f"unknown job kind {kind!r}"},
                token)


def _stream(token: str) -> None:
    req = urllib.request.Request(_server() + "/jobs/events?kinds=" + ",".join(KINDS), headers={
        "Authorization": "Bearer " + token, "Accept": "text/event-stream",
        "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=READ_TIMEOUT_S) as r:
        print(f"connected to {_server()}", flush=True)
        event, data = "", []
        for raw in r:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
            elif not line:
                if event == "job" and data:
                    try:
                        job = json.loads("\n".join(data))
                    except ValueError:
                        job = None
                    if isinstance(job, dict):
                        threading.Thread(target=_handle, args=(job, token), daemon=True).start()
                event, data = "", []


def pair(code: str) -> int:
    got = _post("/pair", {"code": code, "device": "p8a Termux jobs"})
    if not got.get("token"):
        print(f"pairing refused: {got}", file=sys.stderr)
        return 1
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(got["token"] + "\n")
    TOKEN_FILE.chmod(0o600)
    print(f"paired as {got.get('device_id')}: on red5 set "
          f"MEDIA_PHONE_JOBS_DEVICE={got.get('device_id')}")
    return 0


def main() -> int:
    if sys.argv[1:2] == ["--pair"] and len(sys.argv) > 2:
        return pair(sys.argv[2])
    backoff = 2.0
    while True:
        token = _token()
        if not token:
            print("no token: pair first (phone-jobs.py --pair <code>)", flush=True)
            time.sleep(60)
            continue
        t0 = time.monotonic()
        try:
            _stream(token)
            why = "the server closed the stream"
        except urllib.error.HTTPError as e:
            why = f"HTTP {e.code}"
        except (OSError, ValueError) as e:
            why = str(e) or e.__class__.__name__
        # A stream that lasted a while was healthy: start the backoff again.
        if time.monotonic() - t0 > 60:
            backoff = 2.0
        print(f"stream ended ({why}); again in {backoff:.0f}s", flush=True)
        time.sleep(backoff)
        backoff = min(backoff * 2, 60.0)


if __name__ == "__main__":
    raise SystemExit(main())
