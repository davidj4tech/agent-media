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

Env: MEDIA_PHONE_JOBS_SERVER (https://red5.sasonica.com),
MEDIA_PHONE_JOBS_TOKEN (else the token file).
"""

from __future__ import annotations

import json
import os
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
    for attempt in range(3):
        try:
            _post("/jobs/result", {"id": job.get("id"), "rc": rc,
                                   "out": out[:MAX_OUT], "err": err[:MAX_OUT]}, token)
            return
        except (OSError, ValueError) as e:
            print(f"job {job.get('id')}: result not delivered ({e})", flush=True)
            time.sleep(2 * (attempt + 1))


def _stream(token: str) -> None:
    req = urllib.request.Request(_server() + "/jobs/events", headers={
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
                        threading.Thread(target=_run, args=(job, token), daemon=True).start()
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
