"""`POST /follow/trace` — where the app's bold was, sentence by sentence.

The server's side of follow-along leaves a record (the now-playing row, the
replay follower's `replay-anchor.log`), the app's does not: it adds its own
transit, skew estimate and per-device lead, and the bold is only ever seen.
So "the bold was ahead or behind — I can't remember which" (David, 14 Oct
2026) had nothing to check. The app now sends each move of the bold here,
in batches, and each batch is written beside the row as it stands when the
batch arrives — play_started_at and the offsets the server has by then — so
the two can be lined up after the fact.

One JSON line per batch in `<state>/follow-trace.log`, the newest
`KEEP_LINES` kept.
"""

from __future__ import annotations

import json
import time

from agent_media_core._paths import state_dir

from . import auth

#: Lines kept in the log: a reply sends a batch every few seconds.
KEEP_LINES = 4000
#: Events taken from one batch.
MAX_EVENTS = 200
#: Fields kept from each event, and from the row.
_EVENT_KEYS = ("t", "msg", "idx", "heard", "elapsed", "delay", "skew", "lead",
               "anchor_age_s", "off", "next_off", "paused", "src")
_ROW_KEYS = ("source_session", "history_id", "play_started_at", "paused_at",
             "current_sentence_idx", "clip_offsets_s", "clip_starts_s",
             "clip_durations_s", "live_pos_s", "live_pos_at")


def _scalar(v):
    return v if isinstance(v, (int, float, str, bool)) or v is None else None


def _row() -> dict:
    try:
        from agent_media_core.state.store import StateStore

        np = StateStore().get_now_playing("speech") or {}
    except Exception:  # noqa: BLE001 — a trace never fails for want of the row
        return {}
    ex = np.get("extras") or {}
    if isinstance(ex, str):
        try:
            ex = json.loads(ex)
        except ValueError:
            ex = {}
    out = {k: ex.get(k) for k in _ROW_KEYS if ex.get(k) is not None}
    if np.get("started_at") is not None:
        out["started_at"] = np.get("started_at")
    return out


def post(body: dict, bearer: str) -> tuple[bool, dict]:
    """`{session, sent, events: [{t, msg, idx, …}]}` → appended to the log."""
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    events = body.get("events")
    if not isinstance(events, list):
        return False, {"error": "events must be a list", "status": 400}
    kept = []
    for e in events[:MAX_EVENTS]:
        if isinstance(e, dict):
            kept.append({k: _scalar(e.get(k)) for k in _EVENT_KEYS if k in e})
    line = {"recv": round(time.time(), 3), "sent": _scalar(body.get("sent")),
            "session": str(body.get("session") or "")[:64],
            "device": auth.device_id(bearer), "events": kept, "row": _row()}
    path = state_dir() / "follow-trace.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
        if path.stat().st_size > KEEP_LINES * 600:
            lines = path.read_text(encoding="utf-8").splitlines()[-KEEP_LINES:]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        return False, {"error": f"could not write the trace ({e})", "status": 500}
    return True, {"kept": len(kept)}
