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
import re
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
#: Messages fetched from the homeserver per page, going back.
PAGE = 50
#: After a failed first read, how long before a room is asked again.
RETRY_S = 60.0
#: A jump to a message (`around`, §6.14) not held pages back this far for it.
AROUND_PAGES = 10
#: Changes the search index has not taken yet, kept at most (`Room.amends`).
AMENDS_MAX = 1000


def sync() -> "matrix.Sync | None":
    """The running loop, for a reader to subscribe to; None when off."""
    return _SYNC[0]


thread_of = matrix.thread_of


# --- the room cache -------------------------------------------------------------

def _get(config: matrix.Config, path: str, params: dict | None = None,
         timeout: float = 10.0) -> dict:
    url = f"{config.homeserver}/_matrix/client/v3{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {config.token}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _put(config: matrix.Config, path: str, body: dict, token: str,
         timeout: float = 15.0) -> dict:
    req = urllib.request.Request(
        f"{config.homeserver}/_matrix/client/v3{path}", method="PUT",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def owner_token() -> str:
    """The owner's own token (`MATRIX_OWNER_TOKEN`, its own device —
    `matrix-owner-login`): what the app sends with. Never @sam's: a message
    typed on the phone must arrive as David, not as the agent."""
    return os.environ.get("MATRIX_OWNER_TOKEN") or ""


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
        # The service a bridge carries this room for (`m.bridge`), or None.
        self.network: dict | None = None
        self.members: dict[str, str] = {}
        self.messages: list[dict] = []
        self._by_id: dict[str, dict] = {}
        # The /messages token for what is older than `messages[0]`; None once
        # the start of the room is reached.
        self.back: str | None = None
        self.loaded = False
        self._failed_at = 0.0
        self.lock = threading.RLock()
        # For the search index (search.py, 17 Oct 2026): `rev` moves on every
        # change to what is held; `amends` is what it cannot see there — a
        # message taken back (None) or one edited that is no longer held
        # (its new parts), by event id, until the index takes them.
        self.rev = 0
        self.amends: dict[str, list[dict] | None] = {}

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
            try:
                self.network = _network(self._get(self.config, f"/rooms/{q}/state"))
            except (urllib.error.URLError, OSError, ValueError):
                pass                 # no service named: listed with the plain rooms
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

    def send(self, text: str) -> tuple[bool, dict]:
        """Send `text` as the owner. The message is in the room's thread at
        once; the sync's copy of it, when it comes, is the same event and is
        kept once."""
        token = owner_token()
        if not token:
            return False, {"error": "this Matrix room is read-only: no owner token "
                                    "(run matrix-owner-login)", "status": 409}
        content = {"msgtype": "m.text", "body": text}
        try:
            got = _put(self.config, f"/rooms/{_q(self.id)}/send/m.room.message/"
                      f"{uuid.uuid4().hex}", content, token)
        except urllib.error.HTTPError as e:
            return False, {"error": f"the homeserver refused it ({e.code})", "status": 502}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return False, {"error": f"the homeserver could not be reached ({e})",
                           "status": 502}
        ev_id = got.get("event_id") or ""
        if ev_id:
            self.on_event({"type": "m.room.message", "event_id": ev_id,
                           "sender": self.owner, "content": content,
                           "origin_server_ts": int(time.time() * 1000)})
        return True, {"session": self.thread, "pane": None, "submitted": True,
                      "event_id": ev_id}

    def on_event(self, ev: dict) -> None:
        """An event from the sync, newest last."""
        with self.lock:
            self._take(ev, front=False)

    def _amend(self, ev_id: str, parts: list[dict] | None) -> None:
        self.amends.pop(ev_id, None)
        self.amends[ev_id] = parts
        while len(self.amends) > AMENDS_MAX:
            self.amends.pop(next(iter(self.amends)))

    def _take(self, ev: dict, *, front: bool) -> None:
        kind = ev.get("type")
        content = ev.get("content") or {}
        if kind == "m.room.name":
            if not front:
                self.name = content.get("name") or ""
                self.rev += 1
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
                self.rev += 1
            if gone:
                self._amend(gone, None)
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
                self.rev += 1
            elif new and not front and rel.get("event_id"):
                self._amend(rel["event_id"], self._parts(ev.get("sender") or "", new))
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
        self.rev += 1

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
            return self._view(self.messages[start:end]), start > 0 or bool(self.back)

    def around(self, ev_id: str, limit: int, most: int) -> tuple[list[dict], bool, bool] | None:
        """A jump from search (§6.14, 17 Oct 2026): `(window, older, newer)`
        holding message `ev_id`, as `transcript.window_around` makes it —
        paging back up to `AROUND_PAGES` for one older than what is held —
        or None when it is not found."""
        from . import transcript

        with self.lock:
            for _ in range(AROUND_PAGES):
                if ev_id in self._by_id or not self.back or not self.older():
                    break
            win = transcript.window_around(self.messages, ev_id, limit, most)
            if win is None:
                return None
            msgs, older, newer = win
            return self._view(msgs), older or bool(self.back), newer

    def _view(self, msgs: list[dict]) -> list[dict]:
        out = [dict(m, sender=dict(m["sender"])) for m in msgs]
        for m in out:            # names learnt since the message came in
            m["sender"]["name"] = self.members.get(m["sender"]["id"]) or m["sender"]["name"]
            if m["role"] == "assistant":
                # The app's "From <name>" note (§6.2.2 `peer`), drawn
                # over an incoming message.
                m["peer"] = {"name": m["sender"]["name"]}
        return out


def _network(state: list) -> dict | None:
    """`{"id", "name"}` of the bridge that carries a room, from its `m.bridge`
    state: one per service, so a bridge's variants are one (mautrix-gmessages'
    `gmessages-sms` and `gmessages-rcs`, "Google Messages (SMS)", are
    `gmessages`, "Google Messages")."""
    if not isinstance(state, list):
        return None
    for ev in state:
        if isinstance(ev, dict) and ev.get("type") in ("m.bridge", "uk.half-shot.bridge"):
            p = (ev.get("content") or {}).get("protocol") or {}
            pid = str(p.get("id") or "")
            if pid:
                name = re.sub(r"\s*\([^)]*\)\s*$", "", str(p.get("displayname") or pid))
                return {"id": pid.split("-")[0], "name": name}
    return None


_ROOMS: dict[str, Room] = {}       # by thread id


_owner = matrix.owner_of


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
                        "network": r.network,
                        "drivable": bool(owner_token())})
            contact = _contact_of(r)
            if contact:
                # The person the room is with (contacts.py): absent when none.
                out[-1]["contact"] = contact
    return out


def _contact_of(r: Room) -> dict | None:
    from . import contacts

    try:
        return contacts.for_room(r)
    except Exception as e:  # noqa: BLE001 — a row without a person, not no rows
        log.warning("matrix %s: contact match failed: %s", r.id, e)
        return None


def _join_speech(thread: str, messages: list[dict]) -> None:
    """Each message's speech (`spoken`, §6.2.2), from the history rows the
    intake wrote for it: held ones are `unheard` with a Play (the room's
    level is quiet unless changed), played ones replayable."""
    from agent_media_core.state import StateStore

    try:
        rows = StateStore().matrix_speech(thread)
    except Exception as e:  # noqa: BLE001 — no Play is the cost, not the thread
        log.warning("matrix %s: speech unreadable: %s", thread[:8], e)
        return
    for m in messages:
        row = rows.get(m["id"])
        if row is None:
            continue
        ex = row["extras"]
        m["spoken"] = {"id": row["id"], "key": "", "at": row["started_at"],
                       **({"unheard": True} if ex.get("held") and not ex.get("heard")
                          else {})}


def envelope(r: Room, *, limit: int, before: str = "", around: str = "",
             most: int = 500) -> dict:
    """The `/conversation/log?session=` answer for a room: the §6.2.2
    messages, and nothing an agent session has (no lines, no turn). With
    `around` (a search hit), the window holding it, as for a session."""
    jump = None
    win = r.around(around, limit, most) if around else None
    if win is not None:
        messages, older, newer = win
        jump = {"id": around, "found": True, "newer": newer}
    else:
        messages, older = r.page(limit, before)
        if around:
            jump = {"id": around, "found": False, "newer": False}
    _join_speech(r.thread, messages)
    out = {"session": r.thread, "lines": [], "messages": messages, "older": older,
           "pending": False, "working": None, "approval": None, "suggestion": "",
           "recap": None, "context": None,
           "room": {"id": r.id, "name": r.title(),
                    "members": [{"id": u, "name": n} for u, n in sorted(r.members.items())]}}
    if jump is not None:
        out["around"], out["newer"] = jump, jump["newer"]
    return out


def _seed_quiet(threads: list[str]) -> None:
    """A room starts at the quiet speech level (David, 9 Oct 2026): every
    message is kept with a Play, nothing read out in the house. Once per
    room (`seeded-quiet.json`), so a level chosen later — even the default,
    which clears the room's own — is never put back."""
    from agent_media_core import speak_priority

    path = matrix.state_dir() / "seeded-quiet.json"
    try:
        seeded = set(json.loads(path.read_text()))
    except (OSError, ValueError):
        seeded = set()
    new = [t for t in threads if t not in seeded]
    if not new:
        return
    for t in new:
        if t not in speak_priority.levels():
            speak_priority.set_level(t, "quiet")
    try:
        path.write_text(json.dumps(sorted(seeded | set(new))))
    except OSError as e:
        log.warning("matrix: seeded list not saved: %s", e)


def _name_of(room_id: str, user: str) -> str:
    r = _ROOMS.get(thread_of(room_id))
    return (r.members.get(user) or "") if r is not None else ""


def _on_event(room_id: str, ev: dict) -> None:
    # Taken even before the room's first read: that read puts its page in
    # front, and a message in both is kept once.
    r = _ROOMS.get(thread_of(room_id))
    if r is not None:
        r.on_event(ev)


def _bridges(env=None) -> set[str]:
    """`MATRIX_OWNER_BRIDGES`: the bridge bots (`@gmessagesbot:ryer.org`)
    whose rooms the canvas reads as the owner."""
    env = os.environ if env is None else env
    return {b.strip() for b in (env.get("MATRIX_OWNER_BRIDGES") or "").split(",") if b.strip()}


def _bridge_sender(user: str, bridges: set[str]) -> bool:
    """A bridge's bot, or one of its puppets: `@gmessagesbot:x` puppets as
    `@gmessages_…:x` (mautrix's naming)."""
    if user in bridges:
        return True
    local, _, server = user.lstrip("@").partition(":")
    return any(local.startswith(b.lstrip("@").split(":")[0].removesuffix("bot") + "_")
               and server == b.partition(":")[2] for b in bridges)


class _Owner:
    """The owner's own loop (proposal step 3, David, 11 Oct 2026: "read as
    me, all bridge rooms"). A bridge's portal rooms are David's — @sam is
    not in them — and appear one per conversation, so they cannot be an
    allow list: the canvas reads them with the owner's token and takes in
    every room a bridge in `MATRIX_OWNER_BRIDGES` is in, accepting the
    bridge's invitations. A room taken in starts quiet like any other."""

    def __init__(self, base: matrix.Config, owner: str, bridges: set[str], intake) -> None:
        self.config = matrix.Config(homeserver=base.homeserver, token=owner_token(),
                                    rooms=set(), timeout_ms=base.timeout_ms)
        self.owner = owner
        self.bridges = bridges
        self.lock = threading.Lock()
        self.sync = matrix.Sync(self.config, state_path=matrix.state_dir() / "sync-owner.json",
                                on_invite=self._invited)
        if intake is not None:
            on_event, _stop = intake.consumer(self.config, name_of=_name_of, commands=False)
            self.sync.subscribe(on_event)
        self.sync.subscribe(_on_event)

    def take(self, room_id: str) -> None:
        with self.lock:
            if room_id in self.config.rooms or thread_of(room_id) in _ROOMS:
                return
            r = Room(room_id, self.config, self.owner)
            _ROOMS[r.thread] = r
            self.config.rooms.add(room_id)
        _seed_quiet([r.thread])

    def _invited(self, room_id: str, events: list) -> None:
        inviter = next((e.get("sender") or "" for e in events
                        if e.get("type") == "m.room.member"
                        and e.get("state_key") == self.owner), "")
        if not _bridge_sender(inviter, self.bridges):
            return                     # not a bridge's room: left for David
        try:
            _post(self.config, f"/rooms/{_q(room_id)}/join", {})
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("matrix: could not join %s: %s", room_id, e)
            return
        self.take(room_id)

    def discover(self) -> None:
        """The bridge rooms David is already in."""
        try:
            joined = _get(self.config, "/joined_rooms").get("joined_rooms") or []
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("matrix: owner's rooms unreadable: %s", e)
            return
        for room_id in joined:
            try:
                members = _get(self.config, f"/rooms/{_q(room_id)}/joined_members")
            except (urllib.error.URLError, OSError, ValueError):
                continue
            if self.bridges & set(members.get("joined") or {}):
                self.take(room_id)

    def start(self) -> None:
        def run() -> None:
            self.discover()
            self.sync.start()
            for r in list(_ROOMS.values()):
                r.load()
        threading.Thread(target=run, name="matrix-owner", daemon=True).start()


def _post(config: matrix.Config, path: str, body: dict, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(
        f"{config.homeserver}/_matrix/client/v3{path}", method="POST",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {config.token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


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
    owner = _owner()
    for room_id in config.rooms:
        r = Room(room_id, config, owner)
        _ROOMS[r.thread] = r
    _seed_quiet(list(_ROOMS))
    if intake is not None:
        on_event, _stop = intake.consumer(config, name_of=_name_of)
        s.subscribe(on_event)
    s.subscribe(_on_event)
    _SYNC[0] = s
    s.start()
    # The rooms' first read off the request path: /targets should not wait on it.
    threading.Thread(target=lambda: [r.load() for r in list(_ROOMS.values())],
                     name="matrix-rooms", daemon=True).start()
    bridges = _bridges()
    if bridges and owner_token():
        _Owner(config, owner, bridges, intake).start()
    print(f"matrix: sync on for {len(config.rooms)} room(s)"
          f"{', speech intake attached' if intake else ''}"
          f"{', and the rooms of ' + ', '.join(sorted(bridges)) if bridges and owner_token() else ''}",
          file=sys.stderr)
