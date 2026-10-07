"""Matrix rooms as threads, from the one `/sync` loop the canvas runs.

docs/proposals/2026-09-23-matrix-in-the-app.md:

* **Step 0** — one loop per token, here, with every reader subscribed to it:
  the speech intake (agent_media_intake_matrix) and the room cache below.
  Started with the canvas, as the radio is; idle unless
  `MATRIX_ACCESS_TOKEN` and `MATRIX_ROOM_ALLOW` are set, and off with
  `MEDIA_MATRIX_SYNC=0` (for a host still running the standalone
  `media-intake-matrix`, which must not share a token with this one).
* **Step 1** — each allowed room is a read-only thread. Its id is a uuid
  made from the room id (:func:`thread_of`), so every route that takes a
  session takes it unchanged; `/targets` lists it with `source: "matrix"`,
  and `/conversation/log` and the thread stream (§11) serve its timeline
  as §6.2.2 messages. The stream's full re-read (every 3 s) carries a new
  message; there is no transcript to watch.

Only rooms on the allow list are ever read: the allow list is the gate,
not encryption (the proposal's §Encryption).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from agent_media_core import matrix

log = logging.getLogger("agent-media.server.matrix")

_SYNC: list = [None]
_NS = uuid.UUID("6f1d3c52-9a0e-4c1b-8f3e-5a7d2b9c4e10")
#: Messages fetched from the homeserver per page, going back.
PAGE = 50
#: After a failed first read, how long before a room is asked again.
RETRY_S = 60.0


def sync() -> "matrix.Sync | None":
    """The running loop, for a reader to subscribe to; None when off."""
    return _SYNC[0]


def thread_of(room_id: str) -> str:
    """The thread id of a room: a uuid, stable for the room, so the session
    routes take it as they are."""
    return str(uuid.uuid5(_NS, room_id))


# --- the room cache -------------------------------------------------------------

def _get(config: matrix.Config, path: str, params: dict | None = None,
         timeout: float = 10.0) -> dict:
    url = f"{config.homeserver}/_matrix/client/v3{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {config.token}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _q(room_id: str) -> str:
    return urllib.parse.quote(room_id, safe="")


_KIND = {"m.image": "sent a photo", "m.video": "sent a video",
         "m.audio": "sent an audio clip", "m.voice": "sent a voice note",
         "m.file": "sent a file", "m.location": "shared a location"}


class Room:
    """One allowed room: its name, who is in it, and its recent messages as
    §6.2.2 messages, oldest first."""

    def __init__(self, room_id: str, config: matrix.Config, owner: str,
                 get=_get) -> None:
        self.id = room_id
        self.thread = thread_of(room_id)
        self.config = config
        self.owner = owner
        self._get = get
        self.name = ""
        self.members: dict[str, str] = {}
        self.messages: list[dict] = []
        self._by_id: dict[str, dict] = {}
        # The /messages token for what is older than `messages[0]`; None once
        # the start of the room is reached.
        self.back: str | None = None
        self.loaded = False
        self._failed_at = 0.0
        self.lock = threading.RLock()

    # -- reading the homeserver --

    def load(self) -> None:
        """Name, members and the newest page. Once; the sync keeps it."""
        with self.lock:
            if self.loaded or time.monotonic() - self._failed_at < RETRY_S:
                return
            q = _q(self.id)
            try:
                joined = self._get(self.config, f"/rooms/{q}/joined_members")
                self.members = {u: (m or {}).get("display_name") or u
                                for u, m in (joined.get("joined") or {}).items()}
            except (urllib.error.URLError, OSError, ValueError) as e:
                log.warning("matrix %s: members unreadable: %s", self.id, e)
            try:
                self.name = self._get(self.config, f"/rooms/{q}/state/m.room.name").get("name") or ""
            except (urllib.error.URLError, OSError, ValueError):
                self.name = ""       # a room with no name: named from its members
            got = self._page(None)
            if got is None:
                self._failed_at = time.monotonic()
                return
            evs, self.back = got
            for ev in evs:           # newest first, as /messages gives them
                self._take(ev, front=True)
            self.loaded = True

    def _page(self, token: str | None) -> tuple[list, str | None] | None:
        params = {"dir": "b", "limit": str(PAGE)}
        if token:
            params["from"] = token
        try:
            data = self._get(self.config, f"/rooms/{_q(self.id)}/messages", params)
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("matrix %s: messages unreadable: %s", self.id, e)
            return None
        chunk = data.get("chunk") or []
        # No `end`, or an empty page: the start of the room.
        return chunk, (data.get("end") if chunk else None)

    def older(self) -> bool:
        """One more page from before the oldest held; False at the start."""
        with self.lock:
            if not self.back:
                return False
            got = self._page(self.back)
            if got is None:
                return False
            evs, self.back = got
            for ev in evs:
                self._take(ev, front=True)
            return bool(evs)

    # -- events into messages --

    def on_event(self, ev: dict) -> None:
        """An event from the sync, newest last."""
        with self.lock:
            self._take(ev, front=False)

    def _take(self, ev: dict, *, front: bool) -> None:
        kind = ev.get("type")
        content = ev.get("content") or {}
        if kind == "m.room.name":
            if not front:
                self.name = content.get("name") or ""
            return
        if kind == "m.room.member":
            uid = ev.get("state_key") or ""
            # Going back, a member event is an old name: the joined list wins.
            if not front and content.get("membership") == "join":
                self.members[uid] = content.get("displayname") or uid
            return
        if kind == "m.room.redaction":
            gone = ev.get("redacts") or content.get("redacts")
            m = self._by_id.pop(gone, None) if gone else None
            if m is not None:
                self.messages.remove(m)
            return
        if kind != "m.room.message" or not content:
            return
        rel = content.get("m.relates_to") or {}
        if rel.get("rel_type") == "m.replace":
            # An edit: the message keeps its id and takes the new words.
            m = self._by_id.get(rel.get("event_id") or "")
            new = content.get("m.new_content") or {}
            if m is not None and new:
                m["parts"] = self._parts(ev.get("sender") or "", new)
                m["edited"] = True
            return
        ev_id = ev.get("event_id") or ""
        if not ev_id or ev_id in self._by_id:
            return
        m = self._message(ev)
        self._by_id[ev_id] = m
        if front:
            self.messages.insert(0, m)
        else:
            self.messages.append(m)

    def _parts(self, sender: str, content: dict) -> list[dict]:
        msgtype = content.get("msgtype") or "m.text"
        body = (content.get("body") or "").strip()
        if msgtype in _KIND:
            text = _KIND[msgtype] + (f": {body}" if msgtype == "m.file" and body else "")
            return [{"type": "text", "text": f"_{text}_"}]
        if msgtype == "m.emote":
            return [{"type": "text", "text": f"_{self.members.get(sender, sender)} {body}_"}]
        return [{"type": "text", "text": body[:32768]}]

    def _message(self, ev: dict) -> dict:
        sender = ev.get("sender") or ""
        return {"id": ev.get("event_id"),
                "role": "user" if sender == self.owner else "assistant",
                "at": round((ev.get("origin_server_ts") or 0) / 1000, 3),
                "parts": self._parts(sender, ev.get("content") or {}),
                "spoken": None, "turn": {"running": False},
                # A room can have more than two people in it: who said it.
                "sender": {"id": sender, "name": self.members.get(sender) or sender}}

    # -- what the app is told --

    def title(self) -> str:
        if self.name:
            return self.name
        me = (self.owner, os.environ.get("MATRIX_SAM_ID") or "")
        others = [n for u, n in sorted(self.members.items()) if u not in me]
        return ", ".join(others) or self.id

    def page(self, limit: int, before: str = "") -> tuple[list[dict], bool]:
        """The newest `limit` messages, or the `limit` before `before`; and
        whether there are older ones."""
        with self.lock:
            while True:
                end = len(self.messages)
                if before:
                    end = next((i for i, m in enumerate(self.messages)
                                if m["id"] == before), -1)
                    if end < 0:
                        return [], False
                if end >= limit or not self.back or not self.older():
                    break
            start = max(0, end - limit)
            out = [dict(m, sender=dict(m["sender"])) for m in self.messages[start:end]]
            for m in out:            # names learnt since the message came in
                m["sender"]["name"] = self.members.get(m["sender"]["id"]) or m["sender"]["name"]
                if m["role"] == "assistant":
                    # The app's "From <name>" note (§6.2.2 `peer`), drawn
                    # over an incoming message.
                    m["peer"] = {"name": m["sender"]["name"]}
            return out, start > 0 or bool(self.back)


_ROOMS: dict[str, Room] = {}       # by thread id


def _owner(env=None) -> str:
    """Who `user` is in a room: `MATRIX_OWNER_ID`, else the first control id
    that is not the agent's own."""
    env = os.environ if env is None else env
    if env.get("MATRIX_OWNER_ID"):
        return env["MATRIX_OWNER_ID"]
    sam = env.get("MATRIX_SAM_ID") or ""
    ids = [i.strip() for i in (env.get("MATRIX_CONTROL_IDS") or "").split(",")]
    return next((i for i in ids if i and i != sam), "")


def room(thread: str) -> Room | None:
    """The room behind a thread id, loaded; None when it is not a room."""
    r = _ROOMS.get(thread)
    if r is not None and not r.loaded:
        r.load()
    return r


def rows(flags: set | frozenset = frozenset()) -> list[dict]:
    """`/targets` rows for the allowed rooms (§6.1): never live, never rested
    or pinned; the last message is the preview line."""
    out = []
    for r in list(_ROOMS.values()):
        if not r.loaded:
            r.load()
        with r.lock:
            last = r.messages[-1] if r.messages else None
            text = ""
            if last:
                text = " ".join(p.get("text") or "" for p in last["parts"]).strip()
                if last["role"] == "assistant":
                    text = f"{last['sender']['name']}: {text}"
            out.append({"session": r.thread, "title": r.title(), "live": False,
                        "pane": None, "at": last["at"] if last else None,
                        "recap": ({"text": text[:300], "at": last["at"], "source": "matrix"}
                                  if last else None),
                        "archived": r.thread in flags, "rested": None, "pinned": False,
                        "harness": "matrix", "source": "matrix", "room": r.id,
                        "drivable": False})
    return out


def envelope(r: Room, *, limit: int, before: str = "") -> dict:
    """The `/conversation/log?session=` answer for a room: the §6.2.2
    messages, and nothing an agent session has (no lines, no turn)."""
    messages, older = r.page(limit, before)
    return {"session": r.thread, "lines": [], "messages": messages, "older": older,
            "pending": False, "working": None, "approval": None, "suggestion": "",
            "recap": None, "context": None,
            "room": {"id": r.id, "name": r.title(),
                     "members": [{"id": u, "name": n} for u, n in sorted(r.members.items())]}}


def _on_event(room_id: str, ev: dict) -> None:
    # Taken even before the room's first read: that read puts its page in
    # front, and a message in both is kept once.
    r = _ROOMS.get(thread_of(room_id))
    if r is not None:
        r.on_event(ev)


def start() -> None:
    if _SYNC[0] is not None or os.environ.get("MEDIA_MATRIX_SYNC", "1") == "0":
        return
    config = matrix.Config.from_env()
    if config is None:
        return
    s = matrix.Sync(config)
    # The speech intake is its own package, installed alongside but not a
    # dependency of this one.
    try:
        import agent_media_intake_matrix as intake
    except ImportError:
        intake = None
    if intake is not None:
        on_event, _stop = intake.consumer(config)
        s.subscribe(on_event)
    owner = _owner()
    for room_id in config.rooms:
        r = Room(room_id, config, owner)
        _ROOMS[r.thread] = r
    s.subscribe(_on_event)
    _SYNC[0] = s
    s.start()
    # The rooms' first read off the request path: /targets should not wait on it.
    threading.Thread(target=lambda: [r.load() for r in list(_ROOMS.values())],
                     name="matrix-rooms", daemon=True).start()
    print(f"matrix: sync on for {len(config.rooms)} room(s)"
          f"{', speech intake attached' if intake else ''}", file=sys.stderr)
