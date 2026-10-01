"""The phone as an agent's eyes and hands: asks an agent makes of the phone (server-contract.md §6.21).

An agent on this host needs one thing only the phone has — "take a photo:
show me the router lights". It asks here (`POST /phone/ask`, the host's own
token: an agent, not a device); every phone holding `/sessions/events?phone=
<kinds>` (§6.13) that can do that kind is handed it in a `phone` frame and
posts a notification, Allow… / Deny. Allow goes through the unlock to the
app, which does it (a photo: the camera, then `/upload`) and answers
(`POST /phone/answer`). The agent's tool long-polls `GET /phone/ask?id=` for
the outcome. Every ask needs a yes; nothing is standing. David, 1 Oct 2026
(docs/proposals/2026-10-01-the-phone-as-eyes-and-hands.md).

    ask(kind, why, session)  → an open ask, or `no_phone` at once when no
                               phone that can do it is listening
    wait(id, wait_s)         → its status, once settled or after wait_s
    answer(id, decision, result, bearer) → a paired device's allow / deny
    cancel(id)               → the tool gave up
    open_asks(kinds)         → the `phone` frame's `asks`
    listening(kinds)         → a stream with these kinds is connected / gone
    version()                → goes up whenever the open set changes

Statuses: `open` → `ok` (with `result`) | `denied` | `failed` (with `error`)
| `timeout` | `cancelled` | `gone` (a restart lost it; only the tool sees
this, as a 404). Every settled ask is a line in `phone-asks.jsonl` under the
state dir — kind, why, thread, outcome, a photo's path; no coordinates.

In memory: an open ask lives `TTL_S[kind]`, and a settled one is kept
`KEEP_S` so the tool's next poll still finds it.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

from . import auth, sessions

#: What a phone can be asked for. Photo first (David, 1 Oct 2026); dnd and
#: location follow.
KINDS = ("photo", "location", "dnd")
#: How long an ask stays open: a photo may need a walk to the router.
TTL_S = {"photo": 300, "location": 120, "dnd": 120}
#: A settled ask stays readable this long.
KEEP_S = 120
#: Longest `why` kept, and longest long-poll.
WHY_MAX = 200
WAIT_MAX_S = 60.0

_COND = threading.Condition()
#: id → ask (open and recently settled).
_ASKS: dict[str, dict] = {}
#: kind → connected streams that can do it.
_LISTENING: dict[str, int] = {}
_VERSION = 0


def _audit_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    d = root / "agent-media"
    d.mkdir(parents=True, exist_ok=True)
    return d / "phone-asks.jsonl"


def _audit(a: dict) -> None:
    line = {k: a.get(k) for k in ("id", "kind", "why", "session", "title", "at",
                                  "status", "device", "settled_at")}
    res = a.get("result") or {}
    if a["kind"] == "photo" and res.get("path"):
        line["path"] = res["path"]
    if a.get("error"):
        line["error"] = a["error"]
    try:
        with _audit_path().open("a") as f:
            f.write(json.dumps(line, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _bump() -> None:
    """The open set changed: tell the streams now."""
    from . import session_events

    session_events.poke()


def _settle(a: dict, status: str, **extra) -> None:
    """Close an open ask. Caller holds `_COND`; audits and wakes waiters."""
    global _VERSION
    a.update(status=status, settled_at=round(time.time(), 3), **extra)
    _VERSION += 1
    _audit(a)
    _COND.notify_all()


def _prune(now: float) -> bool:
    """Time out what expired and forget what settled long ago; whether the
    open set changed. Caller holds `_COND`."""
    changed = False
    for i, a in list(_ASKS.items()):
        if a["status"] == "open" and a["expires"] <= now:
            _settle(a, "timeout")
            changed = True
        elif a["status"] != "open" and a.get("settled_at", now) + KEEP_S <= now:
            del _ASKS[i]
    return changed


def _title_of(session: str) -> str | None:
    try:
        for r in sessions.cached_states()[0]:
            if r.get("session") == session:
                return str(r.get("title") or "") or None
    except Exception:  # noqa: BLE001 — no title is not a reason to refuse
        pass
    return None


def _public(a: dict) -> dict:
    """An ask as the frame and the tool see it."""
    return {k: a[k] for k in ("id", "kind", "why", "session", "title", "at", "expires",
                              "status", "result", "error") if a.get(k) is not None}


def ask(kind, why, session=None) -> tuple[bool, dict]:
    """`POST /phone/ask {kind, why, session?}` — the caller is the host
    (app.py checks the token). One open ask per session: a second replaces
    the first. No phone listening for `kind`: settled `no_phone` at once."""
    if kind not in KINDS:
        return False, {"status": 400, "error": f"kind is one of {', '.join(KINDS)}"}
    why = " ".join(str(why or "").split())[:WHY_MAX]
    if not why:
        return False, {"status": 400, "error": "say why, in one sentence"}
    if session in (None, ""):
        session = None
    elif not isinstance(session, str) or not sessions._SESSION.fullmatch(session):
        return False, {"status": 400, "error": "not a session id"}
    now = time.time()
    rec = {"id": secrets.token_hex(4), "kind": kind, "why": why, "session": session,
           "title": _title_of(session) if session else None, "at": round(now, 3),
           "expires": round(now + TTL_S[kind], 3), "status": "open"}
    global _VERSION
    with _COND:
        _prune(now)
        if not _LISTENING.get(kind):
            _ASKS[rec["id"]] = rec
            _settle(rec, "no_phone")
            return True, {"ask": _public(rec)}
        if session:
            for a in _ASKS.values():
                if a["status"] == "open" and a.get("session") == session:
                    _settle(a, "cancelled", error="replaced by a newer ask")
        _ASKS[rec["id"]] = rec
        _VERSION += 1
    _bump()
    return True, {"ask": _public(rec)}


def wait(ask_id, wait_s: float = 0.0) -> tuple[bool, dict]:
    """`GET /phone/ask?id=&wait=` — the ask, once settled or after `wait_s`
    (capped at WAIT_MAX_S). 404 when unknown (a restart: the tool says gone)."""
    deadline = time.monotonic() + max(0.0, min(WAIT_MAX_S, float(wait_s or 0)))
    changed = False
    with _COND:
        while True:
            changed |= _prune(time.time())
            a = _ASKS.get(ask_id) if isinstance(ask_id, str) else None
            if a is None:
                out = (False, {"status": 404, "error": "no such ask"})
                break
            left = deadline - time.monotonic()
            if a["status"] != "open" or left <= 0:
                out = (True, {"ask": _public(a)})
                break
            # Wake at the expiry too, so a timeout is seen on time.
            _COND.wait(timeout=max(0.01, min(left, a["expires"] - time.time())))
    if changed:
        _bump()
    return out


def answer(ask_id, decision, result, bearer: str) -> tuple[bool, dict]:
    """`POST /phone/answer {id, decision: allow|deny, result?, error?}` — a
    paired device. Allowed with an `error`: the phone could not do it."""
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if decision not in ("allow", "deny"):
        return False, {"status": 400, "error": "decision is allow or deny"}
    device = str(user.get("username") or "").removeprefix("device:") or "a device"
    with _COND:
        _prune(time.time())
        a = _ASKS.get(ask_id) if isinstance(ask_id, str) else None
        if a is None or a["status"] != "open":
            # Answered by another phone, timed out, or cancelled.
            return False, {"status": 409, "error": "not open",
                           "ask": _public(a) if a else None}
        if decision == "deny":
            _settle(a, "denied", device=device)
        elif isinstance(result, dict) and result.get("error"):
            _settle(a, "failed", device=device, error=str(result["error"])[:300])
        else:
            res = result if isinstance(result, dict) else {}
            if a["kind"] == "photo" and not str(res.get("path") or ""):
                _settle(a, "failed", device=device, error="no photo came back")
            else:
                _settle(a, "ok", device=device, result=res)
        out = _public(a)
    _bump()
    return True, {"ask": out}


def cancel(ask_id) -> tuple[bool, dict]:
    """`POST /phone/cancel {id}` — the host: the tool gave up."""
    with _COND:
        _prune(time.time())
        a = _ASKS.get(ask_id) if isinstance(ask_id, str) else None
        if a is None or a["status"] != "open":
            return False, {"status": 404, "error": "no such open ask"}
        _settle(a, "cancelled")
    _bump()
    return True, {"id": ask_id}


def open_asks(kinds) -> list[dict]:
    """Every open ask of these kinds, oldest first: the `phone` frame."""
    want = set(kinds or ())
    now = time.time()
    with _COND:
        rows = [_public(a) for a in _ASKS.values()
                if a["status"] == "open" and a["expires"] > now and a["kind"] in want]
    return sorted(rows, key=lambda a: (a["at"], a["id"]))


def kinds_of(raw: str | None) -> tuple[str, ...] | None:
    """`?phone=photo,dnd` as kinds; None when not asked."""
    if raw is None:
        return None
    return tuple(k for k in (s.strip() for s in raw.split(",")) if k in KINDS)


def listening(kinds, on: bool) -> None:
    """A stream that can do `kinds` connected (`on`) or went."""
    with _COND:
        for k in kinds or ():
            _LISTENING[k] = max(0, _LISTENING.get(k, 0) + (1 if on else -1))


def version() -> int:
    global _VERSION
    with _COND:
        if _prune(time.time()):
            _VERSION += 1
        return _VERSION


def _reset_for_tests() -> None:
    global _VERSION
    with _COND:
        _ASKS.clear()
        _LISTENING.clear()
        _VERSION = 0
