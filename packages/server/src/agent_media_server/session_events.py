"""The session list as a stream: `GET /sessions/events` (server-contract.md §6.13).

What a phone's background notifier holds open while the app is closed: one
connection that says when any live session changes state — a turn ended
(`working` → `waiting`: "New reply"), a dialog appeared (`approval`: "Needs
you"). The per-thread stream (§11) is one thread's detail; this is every
thread's one word.

    sessions   {"sessions": [{"session", "title", "state"}], "at"}
               — first frame on every connection, then whenever a row's
               session, state or title changes (the whole list: it is small)
    ping       {} after `?ping=` seconds of silence (15–300, default 15)

`state` is `/sessions/state`'s (§6.1): `working` | `waiting` | `approval`.
The client diffs; the server keeps no history per client and a reconnect's
first frame is the catch-up.

**One watcher**, shared by every connection, started by the first subscriber
and stopped when the last leaves. It reads `sessions.cached_states()` — the
same 3 s-cached sweep `/sessions/state` and the app's list already keep warm —
every `POLL_S`, so a stream adds no sweep of its own while the app polls, and
exactly one every `POLL_S` while it is closed.

**Battery.** The client picks the ping interval: a phone asks for a long one
(the notifier asks 120 s), so an idle connection wakes the radio only for
real changes and the occasional ping. The bearer is checked again every
`AUTH_RECHECK_S`: a revoked device's stream ends, and its reconnect gets the
401.

Caps: `MAX_TOTAL` connections; over it, 503. Each holds one of the canvas's
handler threads, and every failed write ends it.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from . import auth, sessions

log = logging.getLogger("agent-media.server.session_events")

#: How often the watcher reads the (cached) sweep.
POLL_S = 3.0
#: Ping bounds and default, in seconds (`?ping=`).
PING_MIN_S = 15.0
PING_MAX_S = 300.0
PING_DEFAULT_S = 15.0
#: The bearer is asked again this often.
AUTH_RECHECK_S = 300.0
#: Streams at once.
MAX_TOTAL = 16


def rows_of(rows: list[dict]) -> list[dict]:
    """What the stream carries of each `/sessions/state` row, in a stable order."""
    out = [{"session": str(r.get("session") or ""), "title": str(r.get("title") or ""),
            "state": str(r.get("state") or "waiting")} for r in rows if r.get("session")]
    return sorted(out, key=lambda r: r["session"])


class _Watcher:
    """The shared sweep reader. `version` goes up when the list changes."""

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.subs = 0
        self.rows: list[dict] | None = None
        self.version = 0
        self.thread: threading.Thread | None = None

    def _read(self) -> None:
        try:
            rows = rows_of(sessions.cached_states()[0])
        except Exception:  # noqa: BLE001 — a failed sweep is not a change
            log.exception("session events: sweep failed")
            return
        with self.cond:
            if rows != self.rows:
                self.rows = rows
                self.version += 1
                self.cond.notify_all()

    def _run(self) -> None:
        while True:
            with self.cond:
                if self.subs <= 0:
                    self.thread = None
                    return
            self._read()
            with self.cond:
                self.cond.wait_for(lambda: self.subs <= 0, timeout=POLL_S)

    def subscribe(self) -> bool:
        with self.cond:
            if self.subs >= MAX_TOTAL:
                return False
            self.subs += 1
            first = self.rows is None or self.thread is None
        if first:
            # The baseline before the first frame, so nothing lands between.
            self._read()
        with self.cond:
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name="session-events",
                                               daemon=True)
                self.thread.start()
        return True

    def unsubscribe(self) -> None:
        with self.cond:
            self.subs -= 1
            if self.subs <= 0:
                self.subs = 0
                # The next first subscriber reads afresh.
                self.rows = None
            self.cond.notify_all()


_W = _Watcher()


def ping_of(raw: str) -> float:
    """`?ping=` in seconds, clamped; the default when absent or not a number."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return PING_DEFAULT_S
    if v != v:  # NaN
        return PING_DEFAULT_S
    return min(PING_MAX_S, max(PING_MIN_S, v))


def serve(h, bearer: str, *, ping_s: float = PING_DEFAULT_S) -> bool:
    """Hold the connection and stream the session list until it goes. Auth
    is the caller's (app.py), done before this. Always True: the request was
    answered, however the stream ended."""
    from .app import _cors, _json

    if not _W.subscribe():
        _json(h, 503, {"ok": False, "error": "too many open streams"})
        return True
    h.close_connection = True
    try:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-store")
        h.send_header("X-Accel-Buffering", "no")
        h.send_header("Connection", "close")
        _cors(h)
        h.end_headers()
        h.wfile.write(b"retry: 5000\n\n")
        h.wfile.flush()
        n = 0

        def send(event: str, data) -> None:
            nonlocal n
            n += 1
            body = json.dumps(data, separators=(",", ":"))
            h.wfile.write(f"id: {n}\nevent: {event}\ndata: {body}\n\n".encode())
            h.wfile.flush()

        seen = -1
        last_sent = time.monotonic()
        checked = time.monotonic()
        while True:
            with _W.cond:
                _W.cond.wait_for(lambda: _W.version != seen and _W.rows is not None,
                                 timeout=max(0.01, ping_s - (time.monotonic() - last_sent)))
                version, rows = _W.version, _W.rows
            if version != seen and rows is not None:
                seen = version
                send("sessions", {"sessions": rows, "at": round(time.time(), 3)})
                last_sent = time.monotonic()
            elif time.monotonic() - last_sent >= ping_s:
                send("ping", {})
                last_sent = time.monotonic()
            if time.monotonic() - checked >= AUTH_RECHECK_S:
                checked = time.monotonic()
                ok, _ = auth.may_control_speech(bearer)
                if not ok:
                    return True
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    except Exception:  # noqa: BLE001 — the headers are out; nothing else may be
        log.exception("session events: stream failed")
    finally:
        _W.unsubscribe()
    return True


def _reset_for_tests() -> None:
    """A fresh watcher: none left subscribed or running from another test."""
    global _W
    _W = _Watcher()
