"""The per-thread event stream: `GET /threads/{session}/events` (§11).

One SSE connection per open thread. It opens with a `snapshot` — the whole
`/conversation/log?session=` envelope plus the session's state — and then
sends only what changed:

    message     {"op": "append" | "replace", "message": <message>}
    live        the follow-along clock of the message being spoken, or null
    working     the running turn's steps, or null
    approval    the dialog the session is stopped on, or null
    suggestion  {"text"}
    state       {"state": "working" | "waiting" | "approval" | "ended",
                 "live": bool, "pane"}
    recap       the latest recap, or null
    ping        {} after PING_S of silence

**One watcher per session**, shared by every connection to it, started by the
first subscriber and stopped when the last one leaves. It does two things:

* **Watches the transcript** by stat, every `POLL_S` (0.3 s). When the file
  grows it waits for the writes to settle (`DEBOUNCE_S`, 0.2 s, never more
  than `DEBOUNCE_MAX_S` from the first change), reads only the new bytes
  (transcript.py's cache) and sends the messages that appeared or changed.
  This is the fast path, and why a turn reaches the phone as it reaches the
  terminal: no speech, no poll interval in the way.
* **Re-reads the rest** — speech (which message is spoken, and the live one),
  working, the dialog, the suggestion, the session's state, the recap —
  every `FAST_S` (1 s) while a turn is working or speech is live, else every
  `SLOW_S` (3 s). Each is sent only when it changed.

The server keeps no history per client. A reconnect gets a fresh snapshot;
`Last-Event-ID` is accepted and ignored. A client that falls `QUEUE_MAX`
events behind is disconnected rather than buffered, and its reconnect's
snapshot is the catch-up. A client applies `message` events by id: an
`append` for an id it already holds (possible in the moment between a
snapshot and the first event) replaces it.

Caps: `MAX_PER_SESSION` connections to one thread, `MAX_TOTAL` in all; over
either, 503. Every connection holds one of the canvas's handler threads, so
a dead client must be noticed: every write that fails ends the connection,
and a `ping` goes out after `PING_S` of silence so a vanished client fails
within one.

Standard library only; the watcher polls with `os.stat` (no inotify).
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time

from . import driver, sessions, threads, transcript

log = logging.getLogger("agent-media.server.thread_events")

#: Transcript stat interval while anyone is watching.
POLL_S = 0.3
#: Quiet time after a transcript write before its messages are sent …
DEBOUNCE_S = 0.2
#: … but never longer than this after the first write of a burst.
DEBOUNCE_MAX_S = 1.0
#: Full re-read interval while a turn is working or speech is live …
FAST_S = 1.0
#: … and otherwise.
SLOW_S = 3.0
#: Silence before a ping.
PING_S = 15.0
#: Connections to one thread, and in all.
MAX_PER_SESSION = 8
MAX_TOTAL = 32
#: Events a connection may fall behind before it is dropped.
QUEUE_MAX = 256

#: The follow-along fields that tick; a message is not "changed" when only
#: these moved — the `live` event carries them.
_TICKING = ("sentence", "elapsed", "server_time", "paused", "delay")


class Subscriber:
    """One connection's end of a watcher: a queue of `(event, data)`."""

    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self.dropped = False

    def put(self, event: str, data) -> None:
        try:
            self.q.put_nowait((event, data))
        except queue.Full:
            # Too far behind: end it; the reconnect's snapshot catches up.
            self.dropped = True


def _sig(msg: dict) -> str:
    """A message's identity for diffing, without its ticking clock."""
    sp = msg.get("spoken")
    if sp and sp.get("live"):
        msg = {**msg, "spoken": {**sp, "live": {k: v for k, v in sp["live"].items()
                                                if k not in _TICKING}}}
    return json.dumps(msg, sort_keys=True)


def _plain(obj, drop=("server_time",)):
    """`obj` without the fields that change on every read (a clock)."""
    if isinstance(obj, dict):
        return {k: v for k, v in obj.items() if k not in drop}
    return obj


def session_state(session: str) -> dict:
    """`{"state", "live", "pane"}` — what the session is doing, from its pane,
    or for a headless session (MEDIA_HEADLESS) from sessiond."""
    pane = sessions.live_sessions().get(session, "")
    if not pane:
        hl = driver.headless_state(session)
        if hl is not None:
            return {"state": hl["state"], "live": hl["live"], "pane": None}
        return {"state": "ended", "live": False, "pane": None}
    st = sessions.activity_of(session, pane).get("state") or "waiting"
    return {"state": st, "live": True, "pane": pane}


def snapshot(session: str) -> tuple[bool, dict]:
    """The first frame: the log envelope plus the session's state (§11)."""
    ok, env = threads.session_log(session)
    if not ok:
        return False, env
    threads.age_live(env)
    st = session_state(session)
    env.update({"state": st["state"], "live": st["live"], "pane": st["pane"],
                "resumable": sessions.session_exists(session)})
    return True, env


class Watcher:
    """Everything one session's subscribers are told, worked out once."""

    def __init__(self, session: str) -> None:
        self.session = session
        self.subs: list[Subscriber] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # What was last sent (or found at start), to diff against.
        self._sigs: dict[str, str] = {}
        self._lines: list = []
        self._live: dict | None = None
        self._live_read = 0.0
        self._last: dict = {}
        self._busy = False
        self._stat = None

    # -- lifecycle --

    def prime(self) -> None:
        """Take the baseline everything later is diffed against. Done on the
        first subscriber's thread *before* it builds its snapshot, so that a
        change landing between the two is sent (at worst twice), never lost."""
        try:
            self._full(emit=False)
        except Exception:  # noqa: BLE001 — a failed read is retried next tick
            log.exception("thread events %s: first read failed", self.session[:8])

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"thread-events-{self.session[:8]}",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _emit(self, event: str, data) -> None:
        with _LOCK:
            subs = list(self.subs)
        for s in subs:
            s.put(event, data)

    # -- the loop --

    def _headless_seq(self) -> int | None:
        """sessiond's event count for this session, when it is headless —
        a new event (a turn starting, a permission request, a result) is
        reason to re-read now rather than at the next 1 s / 3 s tick. None
        for a pane session, or with MEDIA_HEADLESS off."""
        if not driver.owned_headless(self.session):
            return None
        from .driver.headless import call

        r = call("events", session=self.session, since=1 << 62, timeout=2.0)
        return int(r["seq"]) if r.get("ok") else None

    def _run(self) -> None:
        stat = self._stat
        first = last = 0.0
        next_full = time.monotonic() + (FAST_S if self._busy else SLOW_S)
        seq = self._headless_seq()
        # While a burst of writes settles, look again after the debounce
        # rather than a whole poll: the wait is the debounce, not both.
        while not self._stop.wait(DEBOUNCE_S if first else POLL_S):
            now = time.monotonic()
            try:
                st = transcript.file_state(self.session)
                if st != stat:
                    stat = st
                    first = first or now
                    last = now
                if first and (now - last >= DEBOUNCE_S or now - first >= DEBOUNCE_MAX_S):
                    first = 0.0
                    self._messages()
                if seq is not None:
                    got = self._headless_seq()
                    if got is not None and got != seq:
                        seq = got
                        next_full = now      # state or approval moved: re-read now
                if now >= next_full:
                    self._full(emit=True)
                    next_full = time.monotonic() + (FAST_S if self._busy else SLOW_S)
            except Exception:  # noqa: BLE001
                log.exception("thread events %s: read failed", self.session[:8])

    def _diff_messages(self, msgs: list[dict], emit: bool) -> None:
        for m in msgs:
            sig = _sig(m)
            old = self._sigs.get(m["id"])
            if old == sig:
                continue
            self._sigs[m["id"]] = sig
            if emit:
                self._emit("message", {"op": "replace" if old is not None else "append",
                                       "message": m})

    def _messages(self) -> None:
        """The fast path: the transcript grew. Only its messages are re-read,
        with the speech lines from the last full read joined on."""
        got = transcript.messages(self.session, limit=threads.MESSAGES_LIMIT)
        if got is None:
            return
        msgs, _older = got
        transcript.join_speech(msgs, self._lines)
        if not self._last.get("state", {}).get("live"):
            for m in msgs:
                m["turn"]["running"] = False
        self._diff_messages(msgs, emit=True)

    def _full(self, emit: bool) -> None:
        if not emit:
            # The file as the baseline read it: growth after this is news.
            self._stat = transcript.file_state(self.session)
        ok, env = threads.session_log(self.session)
        if not ok:
            return
        threads.age_live(env)
        st = session_state(self.session)
        self._lines = env.get("lines") or []
        self._diff_messages(env.get("messages") or [], emit)
        live = transcript.live_of(env.get("messages") or [])
        if self._live_changed(live) and emit:
            self._emit("live", live)
        self._live, self._live_read = live, time.time()
        now = {"working": env.get("working"), "approval": env.get("approval"),
               "suggestion": {"text": env.get("suggestion") or ""},
               "state": st, "recap": env.get("recap")}
        for name, value in now.items():
            if name in self._last and _plain(self._last[name]) == _plain(value):
                continue
            first = name not in self._last
            self._last[name] = value
            if emit and not first:
                self._emit(name, value)
        self._busy = bool(env.get("working")) or bool(live) or st["state"] == "working"

    def _live_changed(self, live: dict | None) -> bool:
        """Whether the follow-along moved in a way the client's own clock
        would not have: a new reply, a new sentence, pause or resume, or the
        clock itself jumping (a skip). Not the clock ticking."""
        old = self._live
        if (old is None) != (live is None):
            return True
        if live is None:
            return False
        for k in ("id", "sentence", "paused", "offsets", "delay"):
            if old.get(k) != live.get(k):
                return True
        if live.get("paused"):
            return False
        expected = (old.get("elapsed") or 0.0) + (time.time() - self._live_read)
        return abs((live.get("elapsed") or 0.0) - expected) > 1.0


_WATCHERS: dict[str, Watcher] = {}
_LOCK = threading.Lock()


def subscribe(session: str) -> tuple[Watcher, Subscriber] | None:
    """A new subscriber to `session`'s watcher (started if it is the first),
    or None when a cap is reached."""
    with _LOCK:
        total = sum(len(w.subs) for w in _WATCHERS.values())
        w = _WATCHERS.get(session)
        if total >= MAX_TOTAL or (w and len(w.subs) >= MAX_PER_SESSION):
            return None
        sub = Subscriber()
        # A watcher in the table always has subscribers: the last one out
        # takes it out (`unsubscribe`).
        start = w is None
        if start:
            w = _WATCHERS[session] = Watcher(session)
        w.subs.append(sub)
    if start:
        w.prime()
        w.start()
    return w, sub


def unsubscribe(w: Watcher, sub: Subscriber) -> None:
    """Take `sub` off; the last one out stops the watcher."""
    with _LOCK:
        try:
            w.subs.remove(sub)
        except ValueError:
            pass
        if not w.subs:
            w.stop()
            if _WATCHERS.get(w.session) is w:
                del _WATCHERS[w.session]


def watching() -> dict[str, int]:
    """`{session: subscribers}` — for tests and for a status line."""
    with _LOCK:
        return {s: len(w.subs) for s, w in _WATCHERS.items()}


def _reset_for_tests() -> None:
    with _LOCK:
        for w in _WATCHERS.values():
            w.stop()
        _WATCHERS.clear()


# --- the connection ---------------------------------------------------------------


def serve(h, session: str) -> None:
    """Hold the connection open and stream `session` to it until it goes.

    Runs on the handler's own thread. Auth and the session check are the
    caller's (app.py), done before any of this; from here on errors are not
    reported in-stream — the connection closes and the reconnect's snapshot
    (or its 404) says what happened.
    """
    got = subscribe(session)
    if got is None:
        from .app import _json

        _json(h, 503, {"ok": False, "error": "too many open threads"})
        return
    w, sub = got
    h.close_connection = True
    try:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-store")
        h.send_header("X-Accel-Buffering", "no")
        h.send_header("Connection", "close")
        from .app import _cors

        _cors(h)
        h.end_headers()
        h.wfile.write(b"retry: 2000\n\n")
        n = 0

        def send(event: str, data) -> None:
            nonlocal n
            n += 1
            body = json.dumps(data, separators=(",", ":"))
            h.wfile.write(f"id: {n}\nevent: {event}\ndata: {body}\n\n".encode())
            h.wfile.flush()

        # Subscribed first, then the snapshot: anything that changes while it
        # is built is queued behind it rather than lost.
        ok, snap = snapshot(session)
        if not ok:
            return
        send("snapshot", snap)
        while not sub.dropped:
            try:
                event, data = sub.q.get(timeout=PING_S)
            except queue.Empty:
                send("ping", {})
                continue
            send(event, data)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        unsubscribe(w, sub)
