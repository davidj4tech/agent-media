"""This server, on the accounts that have a device here (roadmap item 20).

sasonica.com's Account page lists the servers an account can open. Nothing
else knows where a server is, so the server says: when an account signs in
here (POST /enrol) and then daily, it POSTs its id, name and addresses to the
account's issuer (`{iss}/api/servers`, the sasonica_oidc module) with that
account's own access token — the refresh token kept at sign-in. An account
whose devices are all gone stops being refreshed and drops off the list after
30 days unheard (the issuer's cron).

Addresses: `api` is where the app reaches this server — MEDIA_PUBLIC_URL,
else the running quick tunnel, else the address the last sign-in came in on.
`web` is the Sasonica web app that talks to it, if one is served
(MEDIA_WEB_APP_URL; red5's preview on :8795). MEDIA_SERVERS_REPORT=0 turns
the reports off.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
import urllib.error
import urllib.request

from agent_media_core._paths import state_dir

from . import devices

_PERIOD_S = 86400.0
_FIRST_S = 120.0
_STARTED: list = [False]


def enabled() -> bool:
    return os.environ.get("MEDIA_SERVERS_REPORT", "1") != "0"


def server_id() -> str:
    """This server's id on the list: made once, kept in the state dir."""
    p = state_dir() / "server-id"
    try:
        got = p.read_text().strip()
        if got:
            return got
    except OSError:
        pass
    sid = secrets.token_urlsafe(12)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(sid + "\n")
    return sid


def _seen_path():
    return state_dir() / "server-report.json"


def remember_base(base: str) -> None:
    """The address a sign-in came in on: the `api` fallback."""
    if not base:
        return
    try:
        p = _seen_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"base": base.rstrip("/")}))
    except OSError:
        pass


def addresses() -> dict:
    api = os.environ.get("MEDIA_PUBLIC_URL", "").strip()
    if not api:
        from . import tunnel
        api = tunnel.current_url()
    if not api:
        try:
            api = str(json.loads(_seen_path().read_text()).get("base") or "")
        except (OSError, ValueError):
            api = ""
    return {"api": api.rstrip("/"), "web": os.environ.get("MEDIA_WEB_APP_URL", "").strip().rstrip("/")}


def describe() -> dict:
    return {"id": server_id(), "name": os.environ.get("MEDIA_SERVER_NAME", "").strip() or socket.gethostname(),
            **addresses()}


def _post(iss: str, access: str, body: dict) -> int:
    req = urllib.request.Request(f"{iss.rstrip('/')}/api/servers", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {access}", "Accept": "application/json",
                                          "Content-Type": "application/json", "User-Agent": "agent-media servers"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def report(device_id: str) -> bool:
    """Tell the issuer of the account that enrolled this device about this
    server. False when it could not (no refresh token, issuer refused)."""
    from . import app, oidc

    iss, rt = devices.refresh_token_of(device_id)
    if not (iss and rt):
        return False
    body = describe()
    try:
        status = _post(iss, app._account_access(device_id, iss), body)
        if status in (401, 403):
            status = _post(iss, app._account_access(device_id, iss, fresh=True), body)
    except (oidc.OidcError, OSError) as e:
        print(f"servers: report for {device_id} failed: {e}", file=sys.stderr)
        return False
    if status != 200:
        print(f"servers: {iss} answered {status} for {device_id}", file=sys.stderr)
    return status == 200


def report_soon(device_id: str, base: str = "") -> None:
    """After a sign-in: remember the address it came in on, report in the background."""
    remember_base(base)
    if enabled():
        threading.Thread(target=report, args=(device_id,), name="servers-report", daemon=True).start()


def report_all() -> int:
    """One report per signed-in account: through its most recently seen
    device that still holds a refresh token. How many went through."""
    best: dict[tuple[str, str], dict] = {}
    for d in devices.list_devices():
        key = (str(d.get("iss") or ""), str(d.get("sub") or ""))
        if not all(key) or not devices.refresh_token_of(str(d.get("id")))[1]:
            continue
        if key not in best or (d.get("last_seen") or 0) > (best[key].get("last_seen") or 0):
            best[key] = d
    return sum(report(str(d["id"])) for d in best.values())


def _loop() -> None:
    wait = _FIRST_S
    while True:
        threading.Event().wait(wait)
        wait = _PERIOD_S
        try:
            report_all()
        except Exception as e:  # noqa: BLE001 — the loop outlives a bad pass
            print(f"servers: daily report failed: {e}", file=sys.stderr)


def start() -> None:
    if _STARTED[0] or not enabled():
        return
    _STARTED[0] = True
    threading.Thread(target=_loop, name="servers-report", daemon=True).start()
