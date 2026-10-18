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

**Updates** (David, 18 Oct 2026). `media update --host p8a` sends a job of
kind `update`, `{id, kind: "update", update: {only, after, force, dry_run}}`:
`git pull --ff-only` of this checkout (refused when it has uncommitted
changes, bounded at 90 s), then, when HEAD moved, the same restart as above
for the services the checkout owns (or `only`), 10 s out and detached, with
the busy checks. Answered as `result: {pulled: {from, to}, moved, lines,
restart}`. A dry run fetches and says what a pull would bring.

Env: MEDIA_PHONE_JOBS_SERVER (https://red5.sasonica.com),
MEDIA_PHONE_JOBS_TOKEN (else the token file), MEDIA_PHONE_JOBS_PYTHON (the
python with agent_media_core, else the checkout's venv), MEDIA_JOBS_CHECKOUT
(the checkout an update pulls, else the one this script lives in).
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
KINDS = ("restart", "update")
#: A restart's fields, and nothing else (server phone_jobs.RESTART_KEYS). An
#: update takes the same four: they are its restart's.
RESTART_KEYS = frozenset({"only", "after", "force", "dry_run"})
#: An update's git steps, each at most this long: the interesting failure is a
#: network that takes the connection and then hangs (cli._REMOTE_FIX).
GIT_TIMEOUT_S = 90
#: An update's restart, when the job does not say when: detached, so the
#: answer gets out before this worker (one of the services) restarts.
UPDATE_AFTER_S = 10.0
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


def checkout() -> "Path | None":
    """The agent-media checkout this worker updates: MEDIA_JOBS_CHECKOUT, else
    the one this script lives in (the service dir links into it), else
    ~/projects/agent-media (a copied service dir)."""
    env = (os.environ.get("MEDIA_JOBS_CHECKOUT") or "").strip()
    if env:
        return Path(env).expanduser()
    for d in Path(__file__).resolve().parents:
        if (d / ".git").exists() and (d / "packages" / "core").is_dir():
            return d
    d = Path.home() / "projects" / "agent-media"
    return d if (d / ".git").exists() else None


def _python() -> str:
    """The python that has agent_media_core: MEDIA_PHONE_JOBS_PYTHON, else the
    checkout's venv (bin/python, or Scripts/python.exe on Windows), else this
    one."""
    env = os.environ.get("MEDIA_PHONE_JOBS_PYTHON", "").strip()
    if env:
        return env
    root = checkout()
    if root is not None:
        for venv in (root / ".venv" / "bin" / "python",
                     root / ".venv" / "Scripts" / "python.exe"):
            if venv.exists():
                return str(venv)
    return sys.executable


def _run_restart(argv: list, timeout: float) -> "tuple[int, str, dict | None]":
    """restart_services.run() through `argv`: (rc, its stderr, its dict)."""
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
    return rc, err or "", result


def _timeout(job: dict, default: float) -> float:
    try:
        return max(5.0, float(job.get("timeout") or default) - 5)
    except (TypeError, ValueError):
        return default - 5


def _restart(job: dict, token: str) -> None:
    """A restart job: restart_services.run() here, its dict as `result`."""
    argv, why = restart_argv(job.get("restart"))
    if argv is None:
        print(f"job {job.get('id')}: refused: {why}", flush=True)
        _answer(job.get("id"), {"rc": 2, "out": "", "err": why}, token)
        return
    t0 = time.monotonic()
    rc, err, result = _run_restart(argv, _timeout(job, 60))
    print(f"job {job.get('id')}: restart {' '.join(argv[4:]) or 'all'}: rc {rc} "
          f"in {time.monotonic() - t0:.1f}s", flush=True)
    body = {"rc": rc, "out": "", "err": err[-MAX_OUT:]}
    if result is not None:
        body["result"] = result
    _answer(job.get("id"), body, token)


def _git(root: Path, *args: str, timeout: float = 30) -> "tuple[int, str]":
    """(rc, output) of `git -C root …`; 124 when it hung past `timeout`."""
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                           text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, f"git {args[0]} timed out after {timeout:.0f} s"
    except OSError as e:
        return 127, f"git: {e}"


def update(spec, budget: float = 175.0, python: str = "") -> "tuple[int, dict | None, str]":
    """An update job: `git pull --ff-only` of this checkout, then, when HEAD
    moved, restart_services.run() for the services it owns (or `only`), with
    the busy checks and, unless `after` says otherwise, 10 s out, detached.
    `(rc, result, why)`: result None and rc 2 for a spec that is not an
    update's (nothing run). A dirty checkout is refused, never stashed or
    reset; a dry run fetches and says what a pull would bring, and its
    restart is a dry run too. `after` 0 means 10 s: an update's restart is
    always detached, as doctor --fix's has been.

    result: {"pulled": {"from", "to"}, "moved", "lines", "restart": run()'s
    dict or None}."""
    argv, why = restart_argv(spec, python)
    if argv is None:
        return 2, None, why.replace("restart:", "update:", 1)
    t0 = time.monotonic()
    lines: list = []
    res = {"pulled": None, "moved": False, "lines": lines, "restart": None}
    root = checkout()
    if root is None:
        lines.append("no agent-media checkout here (MEDIA_JOBS_CHECKOUT): nothing to pull")
        return 1, res, ""
    rc, out = _git(root, "status", "--porcelain")
    if rc != 0:
        lines.append(f"{root}: git status failed: {out[-300:]}")
        return 1, res, ""
    if out:
        lines.append(f"{root}: uncommitted changes, not pulled (nothing restarted)")
        return 1, res, ""
    _, before = _git(root, "rev-parse", "--short", "HEAD")
    dry = spec.get("dry_run") is True
    if dry:
        rc, out = _git(root, "fetch", "--quiet", timeout=GIT_TIMEOUT_S)
        if rc != 0:
            lines.append(_pull_failed(rc, out, before))
            return 1, res, ""
        _, after = _git(root, "rev-parse", "--short", "@{u}")
        _, n = _git(root, "rev-list", "--count", "HEAD..@{u}")
        res["pulled"] = {"from": before, "to": after}
        res["moved"] = before != after
        lines.append(f"would pull {before} -> {after} ({n} commit(s))" if before != after
                     else f"already at {before}")
    else:
        rc, out = _git(root, "pull", "--ff-only", "--quiet", timeout=GIT_TIMEOUT_S)
        if rc != 0:
            lines.append(_pull_failed(rc, out, before))
            return 1, res, ""
        _, after = _git(root, "rev-parse", "--short", "HEAD")
        res["pulled"] = {"from": before, "to": after}
        res["moved"] = before != after
        lines.append(f"pulled {before} -> {after}" if before != after
                     else f"already at {before}")
    if not res["moved"]:
        lines.append("nothing to restart")
        return 0, res, ""
    if "--after" not in argv:
        # 0 (or none) is the default: an update's restart is always detached.
        argv += ["--after", str(UPDATE_AFTER_S)]
    left = max(5.0, budget - (time.monotonic() - t0))
    rc, err, result = _run_restart(argv, left)
    res["restart"] = result
    if result is None:
        lines.append(f"restart_services could not run (rc {rc}): "
                     f"{' '.join(err.strip().splitlines()[-3:])}")
    return rc, res, err


def _pull_failed(rc: int, out: str, at: str) -> str:
    if rc == 124:
        return f"FAILED: no route to its git remote (pull timed out at {at})"
    return f"FAILED: {' '.join(out.split())[:500]}"


def _update(job: dict, token: str) -> None:
    t0 = time.monotonic()
    rc, result, err = update(job.get("update"), _timeout(job, 180))
    if result is None:
        print(f"job {job.get('id')}: refused: {err}", flush=True)
        _answer(job.get("id"), {"rc": 2, "out": "", "err": err}, token)
        return
    print(f"job {job.get('id')}: update: {'; '.join(result['lines'])}: rc {rc} "
          f"in {time.monotonic() - t0:.1f}s", flush=True)
    _answer(job.get("id"), {"rc": rc, "out": "", "err": err[-MAX_OUT:],
                            "result": result}, token)


def _handle(job: dict, token: str) -> None:
    kind = job.get("kind") or "cmd"
    if kind == "cmd":
        _run(job, token)
    elif kind == "restart":
        _restart(job, token)
    elif kind == "update":
        _update(job, token)
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
