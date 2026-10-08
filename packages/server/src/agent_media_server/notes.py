"""Notifications an agent on this host puts in the phone's shade (server-contract.md §6.23).

Roadmap item 15 (#5 of "the phone dials out"): red5 used to reach into the
phone with `ssh p8a termux-notification` — a `converse` question ("Sam is
asking"), and the "N spoken replies didn't reach this phone" note. Now the
note is posted here and travels down the stream the phone already holds open:

    POST /notes {id, title, text, priority?, ttl_s?, keep?} (host token)
                     → {ok, note, listening, seen}
    POST /notes/clear {id} (host token) → {ok, cleared}
    /sessions/events?notes=1 → `notes` frames: {"notes": [...]}, every open
                     note, on connecting if any and whenever the set changes

The app posts a note whose id (or text) is new, and takes one down when it
leaves the set — unless it was `keep`: the missed-speech note stays in the
shade until it is swiped, even after it expires here. Posting the same id
again replaces it (a count going up).

`listening`: how many `notes` streams are connected now — a note posted
while one is has been handed to a phone. `seen`: whether any device has ever
asked for notes on this host (a stamp in the state dir), which is how a
caller tells "the phone is asleep, it gets this on waking" from "the app on
the phone is too old for notes" and keeps its old ssh path for the second.

In memory: a canvas restart forgets the open notes. Both producers repost
(the missed-speech retrier every minute; a question is short-lived).
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

ID_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,63}")
PRIORITIES = ("default", "high")
TITLE_MAX = 120
TEXT_MAX = 1000
TTL_DEFAULT_S = 3600.0
TTL_MAX_S = 24 * 3600.0
#: Most open at once: a runaway producer cannot fill the shade.
MAX_OPEN = 20

_LOCK = threading.Lock()
_NOTES: dict[str, dict] = {}
_LISTENING = 0
_VERSION = 0


def _stamp_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    d = root / "agent-media"
    d.mkdir(parents=True, exist_ok=True)
    return d / "notes-seen"


def seen() -> bool:
    """Has a device that shows notes ever connected here?"""
    return _stamp_path().exists()


def _bump() -> None:
    from . import session_events

    session_events.poke()


def _prune(now: float) -> bool:
    """Drop expired notes; whether the set changed. Caller holds `_LOCK`."""
    gone = [i for i, n in _NOTES.items() if n["until"] <= now]
    for i in gone:
        del _NOTES[i]
    return bool(gone)


def post(body) -> tuple[bool, dict]:
    """Open (or replace) one note."""
    global _VERSION
    if not isinstance(body, dict):
        return False, {"status": 400, "error": "a JSON object"}
    nid = str(body.get("id") or "")
    if not ID_RE.fullmatch(nid):
        return False, {"status": 400, "error": "id: [a-z0-9._:-], up to 64"}
    title = " ".join(str(body.get("title") or "").split())[:TITLE_MAX]
    text = str(body.get("text") or "").strip()[:TEXT_MAX]
    if not title and not text:
        return False, {"status": 400, "error": "a title or a text"}
    priority = str(body.get("priority") or "default")
    if priority not in PRIORITIES:
        return False, {"status": 400, "error": "priority: default or high"}
    try:
        ttl = float(body.get("ttl_s") or TTL_DEFAULT_S)
    except (TypeError, ValueError):
        return False, {"status": 400, "error": "ttl_s: seconds"}
    if ttl != ttl or ttl <= 0:
        return False, {"status": 400, "error": "ttl_s: seconds"}
    now = time.time()
    note = {"id": nid, "title": title, "text": text, "priority": priority,
            "keep": bool(body.get("keep")), "at": round(now, 3),
            "until": round(now + min(ttl, TTL_MAX_S), 3)}
    with _LOCK:
        _prune(now)
        if nid not in _NOTES and len(_NOTES) >= MAX_OPEN:
            return False, {"status": 429, "error": "too many open notes"}
        _NOTES[nid] = note
        _VERSION += 1
        listening = _LISTENING
    _bump()
    return True, {"note": note, "listening": listening, "seen": seen()}


def clear(nid) -> tuple[bool, dict]:
    global _VERSION
    with _LOCK:
        gone = _NOTES.pop(str(nid or ""), None) is not None
        if gone:
            _VERSION += 1
        listening = _LISTENING
    if gone:
        _bump()
    return True, {"cleared": gone, "listening": listening, "seen": seen()}


def open_notes() -> list[dict]:
    """Every open note, oldest first: the `notes` frame."""
    with _LOCK:
        _prune(time.time())
        rows = [dict(n) for n in _NOTES.values()]
    return sorted(rows, key=lambda n: (n["at"], n["id"]))


def listening(on: bool) -> None:
    """A `notes` stream connected (`on`) or went."""
    global _LISTENING
    with _LOCK:
        _LISTENING = max(0, _LISTENING + (1 if on else -1))
    if on and not seen():
        try:
            _stamp_path().write_text(f"{time.time():.0f}\n")
        except OSError:
            pass


def version() -> int:
    global _VERSION
    with _LOCK:
        if _prune(time.time()):
            _VERSION += 1
        return _VERSION


def _reset_for_tests() -> None:
    global _VERSION, _LISTENING
    with _LOCK:
        _NOTES.clear()
        _LISTENING = 0
        _VERSION = 0
