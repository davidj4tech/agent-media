"""Who is asking: the Audiobookshelf login as the app's credential.

Moved out of the canvas's reply.py. The design is in
`docs/proposals/2026-09-04-reply-from-the-player.md`; the two points of it
that live here:

* **The ABS login is the credential.** The phone sends the bearer it already
  holds; we hand that bearer straight back to ABS's own `POST /api/authorize`
  and believe what it says. No secret is provisioned to the phone, and the
  amux token stays exactly as it was for the browser and the OWUI pipe. Using
  the *caller's* token (not the service token) to look the item up is
  deliberate: ABS then enforces its own library permissions for us.
* **Typing is a capability, not a role.** ABS has no permission meaning "may
  type into an agent", so the allow-list lives here — root by default, anyone
  else by name in `MEDIA_REPLY_USERS`.

Kept apart from everything else on purpose: at the ABS exit this module is a
deletion, and the gate in front of the routes is what replaces it.

Config (env):
  MEDIA_REPLY_USERS   comma-separated ABS usernames allowed to type (in
                      addition to root)
  MEDIA_REPLY_ROOT    "0" to stop treating the ABS root account as allowed
  MEDIA_ABS_URLS      extra Audiobookshelf servers to ask, comma-separated
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# --- who is asking ------------------------------------------------------------

# ABS is asked once per token per minute, not once per keystroke — /authorize
# is a database read on their side and this sits in the path of a POST a human
# made by hand.
_IDENT_TTL_S = 60.0
_IDENT_LOCK = threading.Lock()
_IDENT: dict[str, tuple[float, dict | None]] = {}


def _abs_url() -> str:
    from agent_media_core import library

    url, _token, _lib = library._abs_cfg()
    return url


def abs_urls() -> list[str]:
    """Every Audiobookshelf this canvas will speak to, likeliest first.

    One host can run more than one server — a second one to try a new client
    against, say — and the app sends the bearer of whichever it is signed in
    to. A bearer means nothing to the server that did not issue it, so "who is
    this?" has to be asked of each in turn rather than only of the one we
    publish to.

    This is an allow-list, and deliberately not built from anything the caller
    says: the caller's own token is forwarded to whatever is on it, so a
    caller-named address would be a way to have us post their login to a host
    of their choosing.

    Extra servers go in `ABS_URLS` in ~/.config/agent-media/abs-bridge.env
    (comma-separated), beside the ABS config that is already there, or in
    MEDIA_ABS_URLS for a one-off. With neither set this is exactly the single
    configured server it always was.
    """
    extra = os.environ.get("MEDIA_ABS_URLS") or ""
    try:
        for line in (Path.home() / ".config" / "agent-media"
                     / "abs-bridge.env").read_text().splitlines():
            line = line.strip()
            if line.startswith("ABS_URLS=") and not extra:
                extra = line.split("=", 1)[1].strip().strip('"\'')
    except OSError:
        pass
    out, seen = [], set()
    for u in [_abs_url()] + extra.split(","):
        u = (u or "").strip().rstrip("/")
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def abs_home(bearer: str) -> str:
    """Which Audiobookshelf this bearer belongs to, if we have found out.

    Only meaningful after `abs_identity`, which is what does the finding; on
    its own it answers with the server we publish to, which is the right guess
    and the only one worth making.
    """
    with _IDENT_LOCK:
        hit = _IDENT.get((bearer or "").strip())
    if hit and time.monotonic() - hit[0] < _IDENT_TTL_S and len(hit) > 2:
        return hit[2]
    return _abs_url()


def _abs_get(url: str, bearer: str, path: str,
             method: str = "GET") -> tuple[dict | None, int]:
    """`(body, status)`. Status 0 means Audiobookshelf could not be reached.

    The status matters: "your token is no good" and "the server did not
    answer" look identical from here otherwise, and they need opposite
    responses — one should send the app off to refresh its token, the other
    must not, because a failed refresh logs the user out.
    """
    req = urllib.request.Request(
        url + path, method=method,
        headers={"Authorization": f"Bearer {bearer}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except (urllib.error.URLError, OSError, ValueError):
        return None, 0


def abs_identity(bearer: str) -> tuple[dict | None, int]:
    """`(user, status)` for this bearer — who ABS says it belongs to.

    Asks ABS the same question the app asks at startup. Only *successes* are
    cached: caching a refusal meant one transient failure refused every reply
    for the next minute, which is exactly how this first went wrong.
    """
    bearer = (bearer or "").strip()
    if not bearer:
        return None, 401
    now = time.monotonic()
    with _IDENT_LOCK:
        hit = _IDENT.get(bearer)
        if hit and now - hit[0] < _IDENT_TTL_S:
            return hit[1], 200
    urls = abs_urls()
    if not urls:
        return None, 0
    # Asked of each server until one recognises the token. A refusal from a
    # server that did not issue it is not news, so a 401 is only the answer
    # once every one of them has said it.
    worst = 0
    for url in urls:
        body, status = _abs_get(url, bearer, "/api/authorize", method="POST")
        user = (body or {}).get("user") if isinstance(body, dict) else None
        user = user if isinstance(user, dict) and user.get("username") else None
        if user:
            with _IDENT_LOCK:
                _IDENT[bearer] = (now, user, url)
            return user, 200
        if status:
            worst = status if worst in (0, 401) or status == 401 else worst
    return None, worst


def _identity_error(status: int) -> dict:
    """Turn "ABS would not tell us who this is" into an answer the app can act
    on.

    401 is the only status that should reach the app as 401, because the app
    answers a 401 by refreshing its token and retrying — and if that refresh
    fails it logs the user out. An Audiobookshelf that is merely down must
    therefore never come back as 401: it would end the session over an outage.
    """
    if status == 401:
        return {"error": "Audiobookshelf rejected that login", "status": 401}
    if status in (0, 502, 503, 504):
        return {"error": "Audiobookshelf did not answer", "status": 503}
    return {"error": f"Audiobookshelf answered {status}", "status": 502}


def may_reply(user: dict | None) -> tuple[bool, str]:
    """Whether this ABS user may type into a session, and why not if not.

    Root is allowed by default: on a single-user server root is the owner, and
    making the sole admin edit a config file to talk to their own agents is
    friction that buys nothing. Everyone else is named explicitly — by
    username, not by type, because `admin` is a library-management role and
    someone trusted with metadata is not thereby trusted with a keyboard.
    """
    if not user:
        return False, "not signed in to Audiobookshelf"
    name = str(user.get("username") or "")
    if user.get("type") == "root" and (os.environ.get("MEDIA_REPLY_ROOT") or "1") != "0":
        return True, name
    allowed = {u.strip() for u in (os.environ.get("MEDIA_REPLY_USERS") or "").split(",") if u.strip()}
    if name in allowed:
        return True, name
    return False, f"{name} is not allowed to reply"


# --- the gates the routes share --------------------------------------------------

def may_control_speech(bearer: str) -> tuple[bool, dict]:
    """The gate for `/speech/ctl`: the same person who may reply may pause."""
    user, status = abs_identity(bearer)
    if not user:
        return False, _identity_error(status)
    ok, why = may_reply(user)
    return (True, {}) if ok else (False, {"error": why, "status": 403})


def _gate(bearer: str) -> tuple[dict | None, dict]:
    """`(user, {})` when this bearer may manage sessions, else `(None, error)`."""
    user, status = abs_identity(bearer)
    if not user:
        return None, _identity_error(status)
    ok, why = may_reply(user)
    if not ok:
        return None, {"error": why, "status": 403}
    return user, {}
