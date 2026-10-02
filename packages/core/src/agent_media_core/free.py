"""Can David be spoken to now? One answer, from what the phone reports.

Roadmap item 14, step 1 (docs/proposals/2026-10-01-speaks-when-youre-free.md).
Speech was held for one reason at a time, each in its own place: a call on
the phone (`Holds`), a silent ringer for alerts (`intake.submit._ringer_hold`),
a reply nobody is looking at (`intake/toast.py`). This is the one place that
says "busy", and why, so the producers on this host can wait for him.

## The facts, and who sends them

The phone sends what only it can see — `POST /device/state` (server-contract
§6.22) with its device token, on every change and every 2 min while busy:

    call           a phone call is under way
    voice          a voice session holds the mic
    quiet          RingerState.quiet(): silent, vibrate, or DND while granted
    meeting_until  the end of a busy calendar event under way (epoch s)
    meeting_title  its title, for the catch-up and for Jev
    manual_until   "busy for an hour" (epoch s)

Each report replaces that device's last one, in ``device-state.json`` under
the state dir. The server writes it; every speech producer reads it.

## Busy, and why

Busy when any device's fresh report gives a reason: ``call``, ``quiet``,
``meeting`` (until it ends), ``manual`` (until it ends). ``night`` (quiet
hours) is not here yet: an open question in the proposal, and the 28 Aug
rule was "the ringer is the state".

**Fails open.** A report older than ``STALE_S`` (5 min, so a heartbeat
missed twice) counts for nothing, and no report means free. A wrongly held
reply is silent by construction and looks like broken speech; a wrongly
spoken one is one sentence at the wrong moment.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from ._paths import state_dir

#: A report older than this is ignored: two missed 2-min heartbeats.
STALE_S = 300.0
#: The reasons, in the order `why` lists them.
REASONS = ("call", "quiet", "meeting", "manual")
#: What a report may carry; anything else is dropped.
_BOOLS = ("call", "voice", "quiet")
_UNTILS = ("meeting_until", "manual_until")
TITLE_MAX = 120


def state_path() -> Path:
    return state_dir() / "device-state.json"


def _load() -> dict:
    try:
        data = json.loads(state_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    os.replace(tmp, path)


def _clean(fields: dict) -> dict:
    out: dict = {}
    for k in _BOOLS:
        if k in fields:
            out[k] = bool(fields[k])
    for k in _UNTILS:
        v = fields.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            out[k] = float(v)
    title = fields.get("meeting_title")
    if isinstance(title, str) and title.strip():
        out["meeting_title"] = title.strip()[:TITLE_MAX]
    return out


def _reasons(rep: dict, now: float) -> "list[str]":
    why = []
    if rep.get("call") or rep.get("voice"):
        why.append("call")
    if rep.get("quiet"):
        why.append("quiet")
    if float(rep.get("meeting_until") or 0) > now:
        why.append("meeting")
    if float(rep.get("manual_until") or 0) > now:
        why.append("manual")
    return why


def report(device: str, fields: dict, now: Optional[float] = None) -> dict:
    """Record one device's facts and return the new answer. Keeps when the
    busy spell began (``busy_since``) across reports, so the catch-up knows
    its window."""
    now = time.time() if now is None else now
    data = _load()
    devices = data.get("devices") if isinstance(data.get("devices"), dict) else {}
    was = answer(now=now, data=data)
    devices[str(device or "device")] = {**_clean(fields if isinstance(fields, dict)
                                                 else {}), "at": now}
    data["devices"] = devices
    after = answer(now=now, data=data)
    if not after["free"] and was["free"]:
        data["busy_since"] = now
    elif after["free"]:
        data.pop("busy_since", None)
    _save(data)
    return answer(now=now, data=data)


def answer(now: Optional[float] = None, data: Optional[dict] = None) -> dict:
    """``{"free", "why", "since", "until", "age_s"}``. ``until`` is the latest
    end among the reasons that have one (a call or a quiet ringer has none:
    null). ``age_s`` is the freshest report's age, null with none."""
    now = time.time() if now is None else now
    data = _load() if data is None else data
    devices = data.get("devices") if isinstance(data.get("devices"), dict) else {}
    why: set[str] = set()
    until: Optional[float] = None
    open_ended = False
    freshest: Optional[float] = None
    for rep in devices.values():
        if not isinstance(rep, dict):
            continue
        at = float(rep.get("at") or 0)
        freshest = at if freshest is None else max(freshest, at)
        if now - at > STALE_S:
            continue
        r = _reasons(rep, now)
        why.update(r)
        for reason, key in (("meeting", "meeting_until"), ("manual", "manual_until")):
            if reason in r:
                until = max(until or 0.0, float(rep[key]))
        if "call" in r or "quiet" in r:
            open_ended = True
    ordered = [r for r in REASONS if r in why]
    return {
        "free": not ordered,
        "why": ordered,
        "since": data.get("busy_since") if ordered else None,
        "until": None if (not ordered or open_ended) else until,
        "age_s": None if freshest is None else int(max(0.0, now - freshest)),
    }


def meeting_title(now: Optional[float] = None) -> str:
    """The title of the meeting making David busy, or ""."""
    now = time.time() if now is None else now
    for rep in (_load().get("devices") or {}).values():
        if (isinstance(rep, dict) and now - float(rep.get("at") or 0) <= STALE_S
                and float(rep.get("meeting_until") or 0) > now):
            return str(rep.get("meeting_title") or "")
    return ""


def held_count(since: Optional[float], store=None) -> int:
    """Speech held because David was busy since ``since``: replies with a
    Play (``held_why == "busy"``) and alerts recorded unspoken
    (``silenced == "busy"``)."""
    if not since:
        return 0
    try:
        if store is None:
            from .state import StateStore
            store = StateStore()
        rows = store.recent_history(sink="speech", limit=500)
    except Exception:  # noqa: BLE001 — a count, never a reason to fail
        return 0
    n = 0
    for r in rows:
        ex = r.get("extras")
        if not isinstance(ex, dict):
            continue
        try:
            started = float(r.get("started_at") or 0)
        except (TypeError, ValueError):
            continue
        if started >= since and (ex.get("held_why") == "busy"
                                 or ex.get("silenced") == "busy"):
            n += 1
    return n


def facts(now: Optional[float] = None) -> "dict[str, str]":
    """`media doctor` lines. Silent on a host no device has reported to."""
    a = answer(now=now)
    if a["age_s"] is None:
        return {}
    out = {"free": "yes" if a["free"] else "no", "free_age_s": str(a["age_s"])}
    if a["why"]:
        out["free_why"] = ",".join(a["why"])
    if a["age_s"] > STALE_S:
        out["free"] = "yes (stale report)"
    return out
