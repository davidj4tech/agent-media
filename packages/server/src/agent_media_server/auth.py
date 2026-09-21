"""The gate in front of every app route: a paired device, else the ABS login.

server-contract.md §9 "Migration": the gate is "a known device token, **or**
an ABS bearer that passes §4.1". This module is that sentence, and the one
place it is said. Every route that used to ask `auth_abs` directly asks here
instead:

  gate(bearer)                → (user, {}) or (None, error)   — may manage sessions
  may_control_speech(bearer)  → (ok, error)                    — the speech bar, harnesses
  identity(bearer)            → (user, status)                 — who, without the may-reply check
  may_reply(user)             → (ok, why)
  abs_bearer(bearer)          → the credential to show ABS for an item lookup

The device check comes first because it is local and cheap: a hash and a
compare against `devices.json`, never a network call, so a device-token
request cannot be answered 503 by an Audiobookshelf that is down. Only a
bearer that is NOT a known device token falls through to `auth_abs`,
unchanged — including a revoked or mistyped device token, which ABS then
refuses with the same 401 an unknown ABS token gets. "Looks like one of ours"
is deliberately not a reason to skip the fallback: an ABS token that happened
to be 43 url-safe characters would otherwise be turned away.

A device is the owner. It carries the rights `may_reply` grants the ABS root
account — whatever `MEDIA_REPLY_ROOT` says, since that switch is about who
ABS's root is, and a device was paired by someone with a shell on this host.
v1 has one scope; see §9.

The fallbacks call `auth_abs` through its module attributes on every call
(`auth_abs._gate`, `auth_abs.may_control_speech`, …), so a test that
monkeypatches `auth_abs.abs_identity` still reaches every route. At the ABS
exit, the fallback lines here are what gets deleted.
"""

from __future__ import annotations

import threading

from . import auth_abs, devices

# The source address of the request this thread is answering, for a device's
# `last_ip`. Set by `app.dispatch` before any route runs; a handler thread
# serves one request at a time, so a thread-local is exactly per-request.
_REQ = threading.local()


def set_client_ip(ip: str) -> None:
    _REQ.ip = ip or ""


def _device(bearer: str) -> dict | None:
    return devices.lookup(bearer, getattr(_REQ, "ip", ""))


def _owner(dev: dict) -> dict:
    """The user a device token stands for: the owner, marked as a device so
    `may_reply` and `abs_bearer` can tell."""
    return {"username": f"device:{dev.get('name') or dev.get('id')}", "type": "root",
            "device": dev.get("id")}


def identity(bearer: str) -> tuple[dict | None, int]:
    """`(user, status)` — `auth_abs.abs_identity`'s shape, device first."""
    dev = _device(bearer)
    if dev:
        return _owner(dev), 200
    return auth_abs.abs_identity(bearer)


def may_reply(user: dict | None) -> tuple[bool, str]:
    if user and user.get("device"):
        return True, str(user.get("username") or "")
    return auth_abs.may_reply(user)


def gate(bearer: str) -> tuple[dict | None, dict]:
    """`(user, {})` when this bearer may manage sessions, else `(None, error)`
    with the error's `status` set (401 / 403 / 502 / 503, per §4.1)."""
    dev = _device(bearer)
    if dev:
        return _owner(dev), {}
    return auth_abs._gate(bearer)


def may_control_speech(bearer: str) -> tuple[bool, dict]:
    """The speech bar's and the harness routes' gate: the same person who may
    reply may pause, or install an agent."""
    if _device(bearer):
        return True, {}
    return auth_abs.may_control_speech(bearer)


def is_device(bearer: str) -> bool:
    return _device(bearer) is not None


def abs_bearer(bearer: str) -> str:
    """What to show Audiobookshelf when looking up an item for this caller.

    An ABS login is used as itself, as it always was — ABS then applies its
    own library permissions. A device token means nothing to ABS and must
    never be sent there (it is a credential for *this* host), so a device's
    item lookups go out under the host's own ABS login instead: a device is
    the owner, and the owner can see every item the host published. With no
    ABS configured this is "", and the lookup finds nothing — the item fields
    stay null, as §10 says they may until the ABS exit.
    """
    if not _device(bearer):
        return bearer
    try:
        from agent_media_core import library

        _url, token, _lib = library._abs_cfg()
        return token or ""
    except Exception:  # noqa: BLE001 — no ABS on this host is not an error
        return ""
