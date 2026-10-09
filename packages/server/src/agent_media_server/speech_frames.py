"""Speech on the phone as frames down its own stream (roadmap item 15, #1).

Until now the server reached the phone's player by dialling it: mpv JSON-IPC
over the tailnet to the app's `MpvServer` on p8a:6614, through
`media-ipc-relay` on 127.0.0.1:16614. Every caller (`sinks/speech.py`, the
follow loop in `intake/submit.py`, `cli.py`'s keys) speaks that protocol, and
most of the traffic is the follow loop reading eight properties a tick at
~1.3 s a round trip.

This takes the relay's port and answers the same protocol here, so no caller
changes, but nothing dials the phone:

* **What changes the player becomes a frame.** Each command is turned into
  ops (`load`, `clear`, `stop`, `pos`, `next`, `prev`, `remove`, `seek`,
  `pause`, `mute`, `volume`, `speed`, `meta`) and one frame per command (an
  `am-claim-play` and the commands riding in it are one) goes down
  `/sessions/events` to the device that asked for `?speech=frames`, woken at
  once (`session_events.poke`). The app applies them to the same player its
  `MpvServer` drives.
* **What reads the player is answered here**, from `Model`: the phone's last
  report (`POST /speech/state`), with every op since applied to it as the
  player would apply it, and `time-pos` run on by the clock while playing.
  A report that predates the last frame sent (its `seq` is behind) moves
  only the playhead, so a jump just made is not undone by old news.
* **The claim** (`am-claim`, `am-claim-play`, `user-data/am-owner`) is kept
  here: there is one server.

With no frame device connected, a connection is passed through untouched to
`MEDIA_SPEECH_FRAMES_UPSTREAM` — the relay, whose spares stay warm — so an
older app build keeps working at the speed it had. That fallback goes when the frames build has proved
itself.

    MEDIA_SPEECH_FRAMES_LISTEN=127.0.0.1:16624    (the canvas starts it)
    MEDIA_SPEECH_FRAMES_UPSTREAM=127.0.0.1:16614  (the relay: fallback, warm)
    MEDIA_SPEECH_SOCKET_SASONICA=tcp://127.0.0.1:16624  (the switch)

A third channel, `handoff` (roadmap item 15, #7; 9 Oct 2026), is the radio's
hand-off player — songs asked of the listener's own music app by name
(core radio_io.HandoffPlayer, the app's HandoffMusic). Same ops, same
`POST /handoff/state`, and the report carries an `extra` object: which app,
which song it took for ours, which way of asking started it, what failed.
It has no upstream: with no device advertising `?handoff=frames` a caller
is answered "no device …" at once, not passed anywhere.

    MEDIA_HANDOFF_FRAMES_LISTEN=127.0.0.1:16626
    MEDIA_RADIO_HANDOFF_ENDPOINT=tcp://127.0.0.1:16626  (the switch)
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from collections import deque
from pathlib import Path

log = logging.getLogger("agent-media.server.speech_frames")

#: Frames kept for a stream that reconnects (`?speech_after=`), and how old.
KEEP = 200
REPLAY_S = 120.0
#: Stored, not played: mpv's own metadata names (MpvServer.isStored).
TITLE = "force-media-title"
USER_DATA = "user-data/"
RINGER = "user-data/agent-media/ringer"
#: The phone's own word beyond the player's (`extra` in its report), read-only.
REPORT = "user-data/agent-media/report"
#: Set and ignored, as the app's MpvServer does.
INERT = ("gapless-audio", "audio-device", "keep-open", "idle")


class Model:
    """The phone's player as this server believes it to be."""

    def __init__(self) -> None:
        self.entries: list[str] = []
        #: What a report cut from `entries`, with any appends since: put back
        #: when a later report counts or plays past the cut (see `report`).
        self.cut: list[str] = []
        self.pos = -1
        self.paused = False
        self.muted = False
        self.volume = 100.0
        self.speed = 1.0
        self.time_pos = -1.0
        self.duration = -1.0
        self.idle = True
        self.eof = False
        self.ringer: dict | None = None
        #: The report's `extra` (the hand-off player's app, song, method, error).
        self.extra: dict = {}
        self.extra_at = 0.0
        #: When `time_pos` was true (monotonic), to run it on while playing.
        self.time_at = time.monotonic()
        self.stored: dict[str, object] = {}

    # --- reads ----------------------------------------------------------------

    def playhead(self) -> float:
        if self.time_pos < 0:
            return -1.0
        t = self.time_pos
        if not self.paused and not self.idle:
            t += (time.monotonic() - self.time_at) * self.speed
        if self.duration > 0:
            t = min(t, self.duration)
        return t

    def path(self) -> str | None:
        return self.entries[self.pos] if 0 <= self.pos < len(self.entries) else None

    # --- the player's own moves, applied as it would apply them ---------------

    def _at(self, t: float) -> None:
        self.time_pos = t
        self.time_at = time.monotonic()

    def apply(self, op: dict) -> None:
        o = op.get("op")
        if o in ("clear", "stop", "remove") or (
                o == "load" and str(op.get("mode") or "replace") == "replace"):
            self.cut = []
        if o == "load":
            uri, mode = str(op.get("uri") or ""), str(op.get("mode") or "replace")
            if self.cut and mode != "replace":
                self.cut.append(uri)
            if mode == "replace":
                self.entries, self.pos, self.idle, self.eof = [uri], 0, False, False
                self.duration = -1.0
                self._at(0.0)
            else:
                self.entries.append(uri)
                if mode == "append-play" and self.idle:
                    self.pos, self.idle, self.eof = len(self.entries) - 1, False, False
                    self.duration = -1.0
                    self._at(0.0)
        elif o == "clear":
            cur = self.path()
            self.entries = [cur] if cur is not None else []
            self.pos = 0 if cur is not None else -1
        elif o == "stop":
            self.entries, self.pos, self.idle, self.eof = [], -1, True, False
            self.duration = -1.0
            self._at(-1.0)
        elif o == "pos":
            i = int(op.get("i", -1))
            if 0 <= i < len(self.entries):
                self.pos, self.idle, self.eof = i, False, False
                self.duration = -1.0
                self._at(0.0)
            elif i < 0:
                self.pos, self.idle = -1, True
                self._at(-1.0)
        elif o in ("next", "prev"):
            self.apply({"op": "pos", "i": self.pos + (1 if o == "next" else -1)})
        elif o == "remove":
            i = int(op.get("i", -1))
            if 0 <= i < len(self.entries):
                del self.entries[i]
                if i < self.pos:
                    self.pos -= 1
                elif i == self.pos:
                    self.pos = min(self.pos, len(self.entries) - 1)
                    if self.pos < 0:
                        self.idle = True
        elif o == "seek":
            self._at(max(0.0, float(op.get("t", 0))))
        elif o == "pause":
            self._at(self.playhead())
            self.paused = bool(op.get("on"))
        elif o == "mute":
            self.muted = bool(op.get("on"))
        elif o == "volume":
            self.volume = float(op.get("v", 100))
        elif o == "speed":
            self._at(self.playhead())
            self.speed = float(op.get("v", 1.0))
        elif o == "meta":
            self.stored[str(op.get("name"))] = op.get("value")

    def report(self, r: dict, current: bool) -> None:
        """The phone's own word. `current`: it has applied every frame sent."""
        if current:
            count = int(r.get("count", len(self.entries)))
            pos = int(r.get("pos", self.pos))
            # A phone that plays an index counts at least that many.
            count = max(count, pos + 1)
            # The phone stopped or cleared by itself: our entries end there.
            # Kept aside all the same: a report once emptied a reply the phone
            # then played through (count 0 while playlist-pos ran 0,1,2,3,
            # 9 Oct 2026), and nothing put the entries back.
            if count < len(self.entries):
                if len(self.entries) > len(self.cut):
                    self.cut = list(self.entries)
                self.entries = self.entries[:count]
            elif count > len(self.entries) and self.cut:
                # The phone has them after all. (Any op but an append clears
                # `cut`, so it is still the list the phone was sent.)
                self.entries = self.cut[:count]
                if count >= len(self.cut):
                    self.cut = []
            self.pos = pos
            self.paused = bool(r.get("paused", self.paused))
            self.muted = bool(r.get("muted", self.muted))
            self.volume = float(r.get("volume", self.volume))
            self.speed = float(r.get("speed", self.speed))
            self.idle = bool(r.get("idle", self.idle))
            self.eof = bool(r.get("eof", self.eof))
        if (current or int(r.get("pos", -2)) == self.pos) and "time_pos" in r:
            self._at(float(r["time_pos"]))
            self.duration = float(r.get("duration", self.duration))
        if isinstance(r.get("ringer"), dict):
            self.ringer = r["ringer"]
        if isinstance(r.get("extra"), dict):
            self.extra = r["extra"]
            self.extra_at = time.time()


class _Hub:
    """The frames, the model, and who is listening."""

    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.lock = threading.Condition()
        self.model = Model()
        #: Callers connected to this channel's port, for their observations.
        self.clients: list["_Client"] = []
        self.clients_lock = threading.Lock()
        self.frames: deque[dict] = deque(maxlen=KEEP)
        # Frames are numbered from now (ms), not from 0: the phone keeps the
        # last seq it saw across a restart of this server, and a seq of its
        # from an earlier run, ahead of ours, made every report it sent look
        # "current" — a report built before the newest frames reached it
        # emptied a reply it then played (9 Oct 2026).
        self.seq = int(time.time() * 1000)
        #: stream id → (device id, connected at); the newest one plays.
        self.listeners: dict[int, tuple[str, float]] = {}
        self._next_listener = 0
        self.reported_at = 0.0
        #: The last load-bearing frame, for trace(): (seq, sent at, state).
        self.traced: dict = {}

    def device(self) -> str | None:
        with self.lock:
            if not self.listeners:
                return None
            return max(self.listeners.values(), key=lambda v: v[1])[0]


def trace(event: str, hub: "_Hub", **kw) -> None:
    """One line in state_dir/frames-timing.log: when a load was sent, when the
    stream delivered it, when the phone first reported on it and first knew
    how long the clip is. A reply whose first clip began 10-16 s after the
    push (#56: replays 12939, 12950, 12957, 12960) left nothing to say which
    hop it was."""
    try:
        from agent_media_core._paths import state_dir
        path = state_dir() / "frames-timing.log"
        line = json.dumps({"at": round(time.time(), 3), "ch": hub.channel,
                           "event": event, **kw}, separators=(",", ":"))
        old = path.read_text().splitlines() if path.exists() else []
        path.write_text("\n".join((old + [line])[-300:]) + "\n")
    except Exception:  # noqa: BLE001 — a trace is never worth a frame
        pass


#: One per player on the phone: speech (6614's), music (6615's) and the
#: radio's hand-off player (the listener's own music app; once 6617's).
CHANNELS = ("speech", "music", "handoff")
_HUBS: dict[str, _Hub] = {c: _Hub(c) for c in CHANNELS}


# --- what the stream and the route call -------------------------------------


def listening(device: str, on: bool, token: int | None = None,
              channel: str = "speech") -> int | None:
    """A stream with `?<channel>=frames` opened (returns its token) or closed."""
    hub = _HUBS[channel]
    with hub.lock:
        if on:
            hub._next_listener += 1
            hub.listeners[hub._next_listener] = (device, time.monotonic())
            return hub._next_listener
        hub.listeners.pop(token, None)
        return None


def playing_stream(token: int | None, channel: str = "speech") -> bool:
    """Whether the stream holding `token` is the one frames go to."""
    hub = _HUBS[channel]
    with hub.lock:
        if token not in hub.listeners:
            return False
        newest = max(hub.listeners.items(), key=lambda kv: kv[1][1])[0]
        return newest == token


def seq(channel: str = "speech") -> int:
    hub = _HUBS[channel]
    with hub.lock:
        return hub.seq


def frames_after(cursor: int, channel: str = "speech") -> list[dict]:
    """Frames newer than `cursor` and young enough to still mean something."""
    hub = _HUBS[channel]
    cutoff = time.time() - REPLAY_S
    with hub.lock:
        return [f for f in hub.frames if f["seq"] > cursor and f["at"] >= cutoff]


def report(device: str, body: dict, channel: str = "speech") -> tuple[bool, dict]:
    """`POST /speech/state` from the device the frames go to."""
    hub = _HUBS[channel]
    if not isinstance(body, dict):
        return False, {"status": 400, "error": "a JSON object, please"}
    if device != hub.device():
        return False, {"status": 409, "error": f"this device is not the one {channel} plays on"}
    with hub.lock:
        try:
            hub.model.report(body, current=int(body.get("seq", -1)) >= hub.seq)
        except (TypeError, ValueError):
            return False, {"status": 400, "error": "malformed state"}
        hub.reported_at = time.time()
        hub.lock.notify_all()
        t = hub.traced
        if t and not t.get("duration"):
            now = time.time()
            if not t.get("reported") and int(body.get("seq", -1)) >= t["seq"]:
                t["reported"] = now
                trace("reported", hub, seq=t["seq"], after_s=round(now - t["at"], 2),
                      pos=body.get("pos"), time_pos=body.get("time_pos"))
            if t.get("reported") and float(body.get("duration") or 0) > 0:
                t["duration"] = now
                trace("duration", hub, seq=t["seq"], after_s=round(now - t["at"], 2),
                      duration=body.get("duration"))
    _notify_observers(hub)
    return True, {"seq": seq(channel)}


def _send(ops: list[dict], hub: "_Hub") -> None:
    """Apply `ops` here and send them to the phone as one frame."""
    if not ops:
        return
    with hub.lock:
        for op in ops:
            hub.model.apply(op)
        hub.seq += 1
        hub.frames.append({"seq": hub.seq, "at": round(time.time(), 3), "ops": ops})
        if any(o.get("op") in ("load", "pos") for o in ops):
            hub.traced = {"seq": hub.seq, "at": time.time()}
            newest = (max(hub.listeners.values(), key=lambda v: v[1])
                      if hub.listeners else None)
            trace("sent", hub, seq=hub.seq, ops=[o.get("op") for o in ops][:6],
                  listeners=len(hub.listeners),
                  stream_age_s=(None if newest is None
                                else round(time.monotonic() - newest[1], 1)))
    from . import session_events
    session_events.poke()
    _notify_observers(hub)


# --- mpv JSON-IPC, answered here --------------------------------------------

_NOT_FOUND = object()


def _stored(name: str) -> bool:
    return name == TITLE or name.startswith(USER_DATA)


def _get(name: str, hub: "_Hub"):
    m = hub.model
    if name == RINGER and m.ringer is not None:
        return m.ringer
    if name == REPORT:
        if not m.extra:
            return _NOT_FOUND
        return {**m.extra, "age_s": round(time.time() - m.extra_at, 1)}
    if name == "pause":
        return m.paused
    if name == "mute":
        return m.muted
    if name == "volume":
        return m.volume
    if name == "speed":
        return m.speed
    if name == "playlist-pos":
        return m.pos
    if name == "playlist-count":
        return len(m.entries)
    if name == "playlist":
        return [{"filename": e, **({"current": True, "playing": True} if i == m.pos else {})}
                for i, e in enumerate(m.entries)]
    if name == "idle-active":
        return m.idle
    if name == "eof-reached":
        return m.eof
    if name in ("path", "filename"):
        p = m.path()
        return _NOT_FOUND if p is None else p
    if name == "time-pos":
        t = m.playhead()
        return _NOT_FOUND if t < 0 else t
    if name == "duration":
        return _NOT_FOUND if m.duration < 0 else m.duration
    if name == "media-title":
        if TITLE in m.stored:
            return m.stored[TITLE]
        p = m.path()
        return _NOT_FOUND if p is None else p.rsplit("/", 1)[-1]
    if _stored(name) or name in INERT:
        v = m.stored.get(name)
        return _NOT_FOUND if v is None else v
    return _NOT_FOUND


def _set_ops(name: str, value, hub: "_Hub") -> list[dict] | None:
    """The ops a `set_property` is, or None for no such property."""
    if name == "pause":
        return [{"op": "pause", "on": _bool(value)}]
    if name == "mute":
        return [{"op": "mute", "on": _bool(value)}]
    if name == "volume":
        return [{"op": "volume", "v": _num(value, 100)}]
    if name == "speed":
        return [{"op": "speed", "v": _num(value, 1.0)}]
    if name == "playlist-pos":
        return [{"op": "pos", "i": int(_num(value, -1))}]
    if _stored(name):
        return [{"op": "meta", "name": name, "value": value}]
    if name in INERT:
        hub.model.stored[name] = value
        return []
    return None


def _bool(v) -> bool:
    if isinstance(v, str):
        return v in ("yes", "true", "1")
    return bool(v)


def _num(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _seek_target(argv: list, hub: "_Hub") -> float | None:
    if len(argv) < 2:
        return None
    try:
        value = float(argv[1])
    except (TypeError, ValueError):
        return None
    mode = (str(argv[2]) if len(argv) > 2 and argv[2] else "relative").split("+")[0]
    m = hub.model
    pos, dur = max(0.0, m.playhead()), m.duration
    if mode == "relative":
        return pos + value
    if mode == "absolute":
        return value
    if dur < 0:
        return None
    if mode == "absolute-percent":
        return dur * value / 100.0
    if mode == "relative-percent":
        return pos + dur * value / 100.0
    return None


def _claim(key: str, mine, now: float, hub: "_Hub"):
    """MpvServer.claim: take `key` unless someone else's deadline is ahead."""
    if not _stored(key) or not isinstance(mine, dict):
        return None
    cur = hub.model.stored.get(key)
    if isinstance(cur, dict):
        owner = str(cur.get("owner") or "")
        if owner and owner != str(mine.get("owner") or "") \
                and _num(cur.get("deadline"), 0) > now:
            return cur
    hub.model.stored[key] = mine
    return mine


def _dispatch(argv: list, ops: list[dict], hub: "_Hub") -> tuple[bool, object]:
    """One command: `(ok, data or error)`, with its ops added to `ops`."""
    verb = str(argv[0])
    m = hub.model
    if verb == "loadfile":
        ops.append({"op": "load", "uri": str(argv[1]),
                    "mode": str(argv[2]) if len(argv) > 2 else "replace"})
        return True, None
    if verb == "stop" or verb in ("quit", "quit-watch-later"):
        ops.append({"op": "stop"})
        return True, None
    if verb == "playlist-clear":
        ops.append({"op": "clear"})
        return True, None
    if verb == "playlist-next":
        ops.append({"op": "next"})
        return True, None
    if verb == "playlist-prev":
        ops.append({"op": "prev"})
        return True, None
    if verb == "playlist-remove":
        at = argv[1] if len(argv) > 1 else None
        i = m.pos if at == "current" else int(_num(at, -1))
        if not 0 <= i < len(m.entries):
            return False, "invalid parameter"
        ops.append({"op": "remove", "i": i})
        return True, None
    if verb in ("get_property", "get_property_string"):
        v = _get(str(argv[1]), hub)
        if v is _NOT_FOUND:
            return False, "property not found"
        if verb == "get_property_string":
            v = ("yes" if v else "no") if isinstance(v, bool) else (
                v if isinstance(v, str) else json.dumps(v))
        return True, v
    if verb in ("set_property", "set_property_string"):
        name = str(argv[1])
        got = _set_ops(name, argv[2] if len(argv) > 2 else None, hub)
        if got is None:
            return False, "property not found"
        ops.extend(got)
        return True, None
    if verb == "seek":
        t = _seek_target(argv, hub)
        if t is None:
            return False, "invalid parameter"
        ops.append({"op": "seek", "t": t})
        return True, None
    if verb == "cycle":
        name = str(argv[1])
        v = _get(name, hub)
        if not isinstance(v, bool):
            return False, "property not found"
        ops.extend(_set_ops(name, not v, hub) or [])
        return True, None
    if verb in ("am-claim", "am-claim-play"):
        mine = argv[2] if len(argv) > 2 else None
        with hub.lock:
            held = _claim(str(argv[1]), mine, _num(argv[3], 0) if len(argv) > 3 else 0, hub)
        if held is None:
            return False, "invalid parameter"
        if held is mine and verb == "am-claim-play" and len(argv) > 4 \
                and isinstance(argv[4], list):
            for sub in argv[4]:
                if isinstance(sub, list) and sub and sub[0] != "am-claim":
                    try:
                        _dispatch(sub, ops, hub)
                    except Exception:  # noqa: BLE001 — one bad command, not the reply
                        pass
        return True, held
    if verb == "client_name":
        return True, "sasonica"
    return False, "invalid parameter"


class _Client:
    """One caller's connection, answered here."""

    def __init__(self, sock: socket.socket, hub: "_Hub") -> None:
        self.sock = sock
        self.hub = hub
        self.out_lock = threading.Lock()
        self.observed: dict[str, object] = {}
        self.last_sent: dict[str, str] = {}

    def send(self, msg: dict) -> None:
        try:
            with self.out_lock:
                self.sock.sendall((json.dumps(msg, separators=(",", ":")) + "\n").encode())
        except OSError:
            pass

    def notify(self, name: str) -> None:
        oid = self.observed.get(name)
        if oid is None:
            return
        with self.hub.lock:
            v = _get(name, self.hub)
        enc = "" if v is _NOT_FOUND else json.dumps(v)
        if self.last_sent.get(name) == enc:
            return
        self.last_sent[name] = enc
        ev = {"event": "property-change", "id": oid, "name": name}
        if v is not _NOT_FOUND:
            ev["data"] = v
        self.send(ev)

    def handle(self, line: str) -> None:
        try:
            req = json.loads(line)
        except ValueError:
            self.send({"error": "invalid parameter"})
            return
        rid = req.get("request_id") if isinstance(req, dict) else None
        cmd = req.get("command") if isinstance(req, dict) else None
        if not isinstance(cmd, list) or not cmd:
            self.send(_answer(rid, False, "invalid parameter"))
            return
        verb = str(cmd[0])
        if verb.startswith("observe_property") and len(cmd) > 2:
            self.observed[str(cmd[2])] = cmd[1]
            self.send(_answer(rid, True, None))
            self.notify(str(cmd[2]))
            return
        if verb == "unobserve_property" and len(cmd) > 1:
            for k in [k for k, v in self.observed.items() if v == cmd[1]]:
                self.observed.pop(k, None)
                self.last_sent.pop(k, None)
            self.send(_answer(rid, True, None))
            return
        ops: list[dict] = []
        try:
            with self.hub.lock:
                ok, data = _dispatch(cmd, ops, self.hub)
        except Exception:  # noqa: BLE001 — one malformed command, not the connection
            ok, data = False, "invalid parameter"
        # The frame first: a caller that has its answer may count on the
        # phone being told already (MpvServer answers after the player acted).
        _send(ops, self.hub)
        self.send(_answer(rid, ok, data))

    def serve(self) -> None:
        with self.hub.clients_lock:
            self.hub.clients.append(self)
        buf = b""
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self.handle(line.decode("utf-8", "replace"))
        except OSError:
            return
        finally:
            with self.hub.clients_lock:
                if self in self.hub.clients:
                    self.hub.clients.remove(self)
            try:
                self.sock.close()
            except OSError:
                pass


def _answer(rid, ok: bool, data) -> dict:
    msg: dict = {}
    if ok and data is not None:
        msg["data"] = data
    msg["error"] = "success" if ok else str(data)
    if rid is not None:
        msg["request_id"] = rid
    return msg


def _notify_observers(hub: "_Hub") -> None:
    with hub.clients_lock:
        clients = list(hub.clients)
    for c in clients:
        for name in list(c.observed):
            c.notify(name)


# --- the listener -------------------------------------------------------------


def _pipe(a: socket.socket, b: socket.socket) -> None:
    """`a`'s bytes to `b` until `a` is done sending; then `b` is told so
    (a half-close), and the answer still comes back the other way."""
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        try:
            b.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _passthrough(client: socket.socket, upstream: tuple[str, int]) -> None:
    """No frame device: the relay's old job, bytes to the phone's own port."""
    try:
        far = socket.create_connection(upstream, timeout=5)
        far.settimeout(None)
    except OSError:
        client.close()
        return
    back = threading.Thread(target=_pipe, args=(far, client), daemon=True)
    back.start()
    _pipe(client, far)
    back.join(timeout=30)
    for s in (client, far):
        try:
            s.close()
        except OSError:
            pass


def _refuse(client: socket.socket, channel: str) -> None:
    """No device and nowhere to pass it: each command answered with why, so a
    caller says so at once rather than reading a closed socket as a hiccup."""
    why = f"no device plays {channel}: none has ?{channel}=frames on its stream"
    buf = b""
    try:
        client.settimeout(30)
        while True:
            chunk = client.recv(65536)
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    req = json.loads(line)
                except ValueError:
                    req = None
                rid = req.get("request_id") if isinstance(req, dict) else None
                client.sendall((json.dumps(_answer(rid, False, why)) + "\n").encode())
    except OSError:
        return
    finally:
        try:
            client.close()
        except OSError:
            pass


def _hostport(s: str) -> tuple[str, int]:
    host, _, port = s.rpartition(":")
    return host or "127.0.0.1", int(port)


def _publish_rtt(port: int, upstream: tuple[str, int] | None, frames: bool) -> None:
    """Where `_mpv_ipc` sizes its breaker for a loopback relay. Answered here,
    the round trip is ~0; passed through to the relay, it is the relay's."""
    try:
        from agent_media_core.entrypoints.ipc_relay import rtt_path
        rtt = 0.005
        if not frames and upstream and upstream[0] in ("127.0.0.1", "localhost"):
            try:
                rtt = float(json.loads(rtt_path(upstream[1]).read_text())["rtt_s"])
            except (OSError, ValueError, KeyError, TypeError):
                pass
        p: Path = rtt_path(port)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"rtt_s": rtt, "frames": frames}))
    except OSError:
        pass


def start(listen: str | None = None, upstream: str | None = None,
          channel: str = "speech") -> socket.socket | None:
    """Listen for `channel`'s callers (`MEDIA_SPEECH_FRAMES_LISTEN`,
    `MEDIA_MUSIC_FRAMES_LISTEN`, `MEDIA_HANDOFF_FRAMES_LISTEN`); None when unset."""
    hub = _HUBS[channel]
    key = f"MEDIA_{channel.upper()}_FRAMES"
    listen = listen or os.environ.get(f"{key}_LISTEN", "")
    if not listen:
        return None
    up = upstream if upstream is not None else os.environ.get(f"{key}_UPSTREAM", "")
    far = _hostport(up) if up else None
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(_hostport(listen))
    srv.listen(32)
    port = srv.getsockname()[1]
    _publish_rtt(port, far, False)

    def loop() -> None:
        mode = None
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            frames = hub.device() is not None
            if frames != mode:
                mode = frames
                _publish_rtt(port, far, frames)
            if frames:
                threading.Thread(target=_Client(c, hub).serve, daemon=True).start()
            elif far is not None:
                threading.Thread(target=_passthrough, args=(c, far), daemon=True).start()
            else:
                threading.Thread(target=_refuse, args=(c, channel), daemon=True).start()

    threading.Thread(target=loop, name=f"{channel}-frames", daemon=True).start()
    print(f"{channel} frames on {listen}" + (f" (else through to {up})" if up else ""),
          flush=True)
    return srv


def _reset_for_tests() -> None:
    for c in CHANNELS:
        _HUBS[c] = _Hub(c)
