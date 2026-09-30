"""The phone as the TV's mic: asks to speak for another device (server-contract.md §6.20).

A TV with no microphone of its own (the living-room Google TV: no
`android.hardware.microphone`, and its remote's mic is kept by Google for its
own search) borrows the phone's. Its mic key does not dictate; it asks here,
and every phone holding `/sessions/events?mic=1` (§6.13) is handed the ask in
a `mic` frame, posts a heads-up notification, and — tapped — opens the same
thread already listening. The words are sent from the phone (`/reply`, §6.2);
the TV just sees them arrive. David, 30 Sep 2026.

    ask(session, bearer)   → an open ask, the asking device's old one replaced
    cancel(id, bearer)     → gone: the TV's Cancel, its words arrived, or the
                             phone took it
    open_asks(exclude)     → every open, unexpired ask but the connecting
                             device's own — the `mic` frame's `asks`
    version()              → goes up whenever that set changes (an expiry
                             included); the stream's watcher reads it

In memory only: an ask lives 90 s, so a server restart dropping them costs a
second tap on the TV at worst.
"""

from __future__ import annotations

import secrets
import threading
import time

from . import auth, sessions

#: How long an ask stays open (the TV's bar and the phone's notification
#: give up at the same 90 s).
TTL_S = 90

_LOCK = threading.Lock()
#: id → ask.
_ASKS: dict[str, dict] = {}
_VERSION = 0


def _key(ask: dict) -> str:
    """Whose ask this is, for one-per-device: the device, else the login."""
    return f"d:{ask['device_id']}" if ask.get("device_id") else f"u:{ask['device']}"


def _prune(now: float) -> bool:
    """Drop expired asks; whether any went. Caller holds `_LOCK`."""
    gone = [i for i, a in _ASKS.items() if a["expires"] <= now]
    for i in gone:
        del _ASKS[i]
    return bool(gone)


def _bump() -> None:
    """The set changed: tell the stream now, not on its next 3 s tick."""
    from . import session_events

    session_events.poke()


def _title_of(session: str) -> str | None:
    """The thread's title if a live session has one (the same cached sweep
    the stream reads), else None — the phone then says "into this chat"."""
    try:
        for r in sessions.cached_states()[0]:
            if r.get("session") == session:
                return str(r.get("title") or "") or None
    except Exception:  # noqa: BLE001 — no title is not a reason to refuse
        pass
    return None


def ask(session, bearer: str) -> tuple[bool, dict]:
    """`POST /mic/ask {"session"?}` — the TV's mic key. `session` absent,
    null or "" is a new chat."""
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if session in (None, ""):
        session = None
    elif not isinstance(session, str) or not sessions._SESSION.fullmatch(session):
        return False, {"status": 400, "error": "not a session id"}
    name = str(user.get("username") or "").removeprefix("device:") or "another device"
    now = time.time()
    rec = {"id": secrets.token_hex(4), "device": name,
           "device_id": user.get("device") or None, "session": session,
           "title": _title_of(session) if session else None,
           "at": round(now, 3), "expires": round(now + TTL_S, 3)}
    global _VERSION
    with _LOCK:
        _prune(now)
        # One open ask per asking device: a second press replaces the first.
        for i in [i for i, a in _ASKS.items() if _key(a) == _key(rec)]:
            del _ASKS[i]
        _ASKS[rec["id"]] = rec
        _VERSION += 1
    _bump()
    return True, {"ask": dict(rec)}


def cancel(ask_id, bearer: str) -> tuple[bool, dict]:
    """`POST /mic/cancel {"id"}` — any gated caller may: the TV that asked, or
    the phone that took it."""
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    global _VERSION
    with _LOCK:
        pruned = _prune(time.time())
        found = isinstance(ask_id, str) and _ASKS.pop(ask_id, None) is not None
        if found or pruned:
            _VERSION += 1
    if found or pruned:
        _bump()
    if not found:
        return False, {"status": 404, "error": "no such open ask"}
    return True, {"id": ask_id}


def open_asks(exclude_device: str | None = None) -> list[dict]:
    """Every open, unexpired ask, oldest first, but those made by
    `exclude_device` (a device id; None or "" excludes nothing)."""
    now = time.time()
    with _LOCK:
        rows = [dict(a) for a in _ASKS.values() if a["expires"] > now
                and not (exclude_device and a.get("device_id") == exclude_device)]
    return sorted(rows, key=lambda a: (a["at"], a["id"]))


def version() -> int:
    """The set's version, after dropping what has expired (an expiry is a
    change: the phone takes that notification down)."""
    global _VERSION
    with _LOCK:
        if _prune(time.time()):
            _VERSION += 1
        return _VERSION


def _reset_for_tests() -> None:
    global _VERSION
    with _LOCK:
        _ASKS.clear()
        _VERSION = 0
