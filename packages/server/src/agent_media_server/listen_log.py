"""`POST /device/listen-log` — what each of the phone's mic listens came to.

David, 25 Oct 2026: "In hands-free, so often the mic doesn't seem to accept
my spoken text, even though it often shows up in the black screen
transcript." The phone keeps its own listen log on its loopback
(Readouts.java, :8774/listen/log), out of reach when it is off the home
network. So the app sends its lines here, outbound, in batches
(sasonica-app lib/listenLog.ts): one per listen — where (`at`), what came back
(`text`, `partial`, `error`, `confidence`) and what the app did with it
(`outcome`: taken, command, echo, error, empty, stale, rescued; `voiceNear`
when a voice was playing). A paired device only.

One JSON line per listen in `<state>/listen-log.jsonl`, with the device's
name and the time it arrived; the newest KEEP_LINES are kept. Read it with
`tail <state>/listen-log.jsonl`.
"""

from __future__ import annotations

import json
import threading
import time

from agent_media_core._paths import state_dir

from . import auth

KEEP_LINES = 5000
#: Lines taken from one batch, and characters kept of any one field.
MAX_LINES = 200
MAX_CHARS = 400
_FIELDS = {"t": (int, float), "at": str, "outcome": str, "text": str, "partial": str,
           "error": str, "confidence": (int, float), "voiceNear": bool, "session": str}
_LOCK = threading.Lock()


def _path():
    return state_dir() / "listen-log.jsonl"


def _clean(line) -> dict | None:
    if not isinstance(line, dict):
        return None
    out = {}
    for k, kind in _FIELDS.items():
        v = line.get(k)
        if isinstance(v, bool) and kind is not bool:
            continue
        if isinstance(v, kind):
            out[k] = v[:MAX_CHARS] if isinstance(v, str) else v
    return out if out.get("outcome") else None


def post(body, bearer: str) -> tuple[bool, dict]:
    """`POST /device/listen-log {lines: [...]}` → `{kept}`."""
    user, err = auth.gate(bearer)
    if not user or not auth.device_id(bearer):
        return False, err or {"status": 401, "error": "a paired device only"}
    lines = body.get("lines") if isinstance(body, dict) else None
    if not isinstance(lines, list):
        return False, {"status": 400, "error": 'expected {"lines": [...]}'}
    device = str(user.get("username") or "").removeprefix("device:") or "a device"
    now = round(time.time(), 3)
    kept = [c for c in (_clean(x) for x in lines[:MAX_LINES]) if c]
    if not kept:
        return True, {"kept": 0}
    path = _path()
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for c in kept:
                f.write(json.dumps({"received": now, "device": device, **c}, ensure_ascii=False) + "\n")
        # Trimmed once it is a fifth over, so most posts only append.
        old = path.read_text(encoding="utf-8").splitlines()
        if len(old) > KEEP_LINES * 1.2:
            path.write_text("\n".join(old[-KEEP_LINES:]) + "\n", encoding="utf-8")
    return True, {"kept": len(kept)}
