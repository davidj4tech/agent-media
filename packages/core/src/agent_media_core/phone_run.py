"""Run a command in the phone's Termux through its worker, as `ssh p8a cmd` would.

    python -m agent_media_core.phone_run '<command>'

What `music_local.phone_argv` hands its callers when `MEDIA_PHONE_JOBS=1`
(roadmap item 15): the command goes to the canvas on this host
(`POST /jobs/run`, the host's own token), which gives it to the phone's
Termux worker over the stream the worker holds open (server phone_jobs.py),
and the answer comes back here. Its stdout and stderr are printed and its
exit code is this one's, so a caller reading `ssh`'s output reads this the
same. 255 is ssh's own "could not reach it", and is what this says when no
worker is connected or the canvas cannot be reached.

stdin is not carried (see phone_jobs).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _token() -> str:
    tok = os.environ.get("AMUX_AUTH_TOKEN", "")
    if tok:
        return "" if tok.lower() == "none" else tok
    try:
        return (Path.home() / ".amux" / "auth_token").read_text().strip()
    except OSError:
        return ""


def _base() -> str:
    raw = (os.environ.get("MEDIA_PHONE_JOBS_URL") or os.environ.get("MEDIA_VISUAL_URL")
           or "http://127.0.0.1:8781")
    return raw.replace(",", " ").split()[0].rstrip("/")


def run(cmd: str, timeout: float | None = None) -> tuple[int, str, str]:
    """`(rc, out, err)` from the phone."""
    if timeout is None:
        try:
            timeout = float(os.environ.get("MEDIA_PHONE_JOBS_TIMEOUT") or 600)
        except ValueError:
            timeout = 600.0
    req = urllib.request.Request(
        _base() + "/jobs/run",
        data=json.dumps({"cmd": cmd, "timeout": timeout}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "X-Auth-Token": _token()})
    try:
        with urllib.request.urlopen(req, timeout=timeout + 15) as r:
            got = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return 255, "", f"phone_run: the canvas said {e.code}"
    except (OSError, ValueError) as e:
        return 255, "", f"phone_run: {e}"
    try:
        return int(got.get("rc", 255)), str(got.get("out") or ""), str(got.get("err") or "")
    except (TypeError, ValueError):
        return 255, "", "phone_run: a malformed answer"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: python -m agent_media_core.phone_run '<command>'", file=sys.stderr)
        return 2
    rc, out, err = run(" ".join(args))
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err if err.endswith("\n") else err + "\n")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
