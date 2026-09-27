"""Sasonica Shell sign-ins approved on the phone (sasonica-shell §6,
`SASONICA_SIGNIN=app`).

When an assistant adds this machine's shell as a connector, its sign-in page
shows a short code and waits. The Worker keeps the waiting sign-in; the
shell's runner on this machine sees it and raises a `needs` alert, so the
phone is told. The phone's Home lists them from here (`/dashboard`'s
`signins`) and a tap on Approve or Deny comes back here, where the decision
is signed with the machine's relay key and handed to the Worker's runner API.
The Worker checks the runner token *and* the signature, so neither the phone
nor anyone on the network holds anything that could grant a shell by itself.

Everything this needs is the shell's own config on this machine
(`~/.config/sasonica/env`, `SASONICA_CONF` to move it): the Worker's URL, the
runner token, and the relay key file. A machine without the shell has no
sign-ins, and says so as an empty list.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import auth

_ID = re.compile(r"[0-9a-f]{32}")
#: How long a listing is reused: Home asks every few seconds, the Worker is a
#: network hop away.
CACHE_S = 5.0
_LOCK = threading.Lock()
_CACHE: tuple[float, list[dict]] = (0.0, [])


def _conf_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get("SASONICA_CONF") or "~/.config/sasonica"))


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            m = re.match(r"\s*([A-Z_][A-Z0-9_]*)=(.*)$", line)
            if m:
                out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _shell() -> dict | None:
    """The Worker URL, runner token and relay key, or None without a shell here."""
    env = _read_env(_conf_dir() / "env")
    url, token = env.get("SASONICA_WORKER_URL", ""), env.get("SASONICA_RUNNER_TOKEN", "")
    key_file = Path(os.path.expanduser(env.get("SASONICA_KEY_FILE") or str(_conf_dir() / "relay.key")))
    try:
        key = key_file.read_text().strip()
    except OSError:
        key = ""
    if not (url.startswith("https://") and token and key):
        return None
    return {"url": url.rstrip("/"), "token": token, "key": key}


def _runner(shell: dict, body: dict, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(
        f"{shell['url']}/runner", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {shell['token']}", "Content-Type": "application/json",
                 "User-Agent": "agent-media shell_signin"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}") | {"_status": e.code}
        except ValueError:
            return {"error": f"the Worker answered {e.code}", "_status": e.code}


def waiting(*, fresh: bool = False) -> list[dict]:
    """The sign-ins waiting for the owner, oldest first: `{id, code, client,
    host, at}`. [] without a shell here or when the Worker will not answer."""
    global _CACHE
    now = time.time()
    with _LOCK:
        if not fresh and now - _CACHE[0] < CACHE_S:
            return list(_CACHE[1])
    shell = _shell()
    rows: list[dict] = []
    if shell:
        try:
            got = _runner(shell, {"op": "signins"}, timeout=4.0).get("signins") or []
        except (OSError, ValueError):
            got = []
        rows = [{"id": str(r.get("id")), "code": str(r.get("code") or ""),
                 "client": str(r.get("client_name") or ""), "host": str(r.get("client_host") or ""),
                 "at": str(r.get("created_at") or "")}
                for r in got if isinstance(r, dict) and _ID.fullmatch(str(r.get("id") or ""))]
    with _LOCK:
        _CACHE = (now, rows)
    return list(rows)


def connect_info(bearer: str) -> tuple[bool, dict]:
    """`GET /shell`: what an assistant needs to connect to this machine's
    shell, for the app's "Connect an assistant" (David, 27 Sep 2026): the
    plain sign-in URL (`<worker>/mcp`, null with OAuth off) and how sign-in is
    approved, and — to a device with the enrol bit only — the shared secret
    URL, which needs no sign-in."""
    ok, detail = auth.may_control_speech(bearer)
    if not ok:
        return False, detail
    env = _read_env(_conf_dir() / "env")
    base = env.get("SASONICA_WORKER_URL", "").rstrip("/")
    signin = "app" if env.get("SASONICA_SIGNIN") == "app" else (
        "access" if env.get("SASONICA_OWNER_EMAIL") else "")
    on = bool(base.startswith("https://") and signin)
    out = {"shell": bool(base), "url": f"{base}/mcp" if on else None,
           "signin": signin or None, "secret_url": None}
    # The shared secret URL (David, 27 Sep 2026: "I wanted the secret URL
    # method") — a password for the shell, so only to a device that may pair
    # others (the owner's), and never logged.
    secret = env.get("SASONICA_URL_SECRET", "")
    enrol_ok, _e = auth.may_enrol(bearer)
    if enrol_ok and secret and base.startswith("https://"):
        out["secret_url"] = f"{base}/{secret}/mcp"
    return True, out


def listing(bearer: str) -> tuple[bool, dict]:
    """`GET /shell/signins`."""
    ok, detail = auth.may_control_speech(bearer)
    if not ok:
        return False, detail
    # Cached a few seconds: every screen of the app asks while it is open.
    return True, {"signins": waiting(), "shell": _shell() is not None}


def decide(sid: str, approve: bool, bearer: str) -> tuple[bool, dict]:
    """`POST /shell/signin {id, approve}`: the owner's answer, signed with the
    relay key. Granting a connector is granting a shell, so it takes a device
    that may enrol others (§9), not just any paired one."""
    ok, detail = auth.may_enrol(bearer)
    if not ok:
        return False, detail
    sid = (sid or "").strip()
    if not _ID.fullmatch(sid):
        return False, {"error": "not a sign-in id", "status": 400}
    shell = _shell()
    if not shell:
        return False, {"error": "Sasonica Shell is not set up on this machine", "status": 409}
    decision = "approve" if approve else "deny"
    sig = hmac.new(shell["key"].encode(), f"signin\n{sid}\n{decision}".encode(),
                   hashlib.sha256).hexdigest()
    try:
        r = _runner(shell, {"op": "signin-decide", "id": sid, "approve": bool(approve), "sig": sig})
    except (OSError, ValueError) as e:
        return False, {"error": f"could not reach the shell's Worker: {e}", "status": 502}
    if r.get("error"):
        return False, {"error": str(r["error"]), "status": int(r.get("_status") or 502)}
    waiting(fresh=True)
    return True, {"id": sid, "status": r.get("status")}


def _reset_for_tests() -> None:
    global _CACHE
    with _LOCK:
        _CACHE = (0.0, [])
