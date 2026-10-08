"""One Matrix `/sync` loop, shared by everything that reads a room.

Two long-polls on the same access token and device lose events: each one
advances `next_batch`, and whatever the other was given never comes back.
So there is exactly one loop per token, and the readers subscribe to it
(docs/proposals/2026-09-23-matrix-in-the-app.md, step 0). On red5 it runs
inside the canvas (agent_media_server.matrix); `media-intake-matrix` runs
the same loop standalone on a host with no canvas.

Standard library only, like the intake it came out of. Only rooms on the
allow list (`MATRIX_ROOM_ALLOW`) ever reach a subscriber.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

DEFAULT_SYNC_TIMEOUT_MS = 30000
DEFAULT_HOMESERVER = "https://matrix.example.org"
_SEEN_KEEP = 100

# A subscriber: (room_id, event). Called on the sync thread, so it must not
# block — anything slow (playing a voice note) belongs on its own worker.
Subscriber = Callable[[str, dict], None]


def _csv(value: str | None) -> set[str]:
    return {v.strip() for v in (value or "").split(",") if v.strip()}


@dataclass
class Config:
    homeserver: str
    token: str
    rooms: set[str]
    timeout_ms: int = DEFAULT_SYNC_TIMEOUT_MS

    @classmethod
    def from_env(cls, env: dict | None = None) -> Optional["Config"]:
        """The loop's config, or None when this host has no Matrix token
        or no room allowed."""
        env = os.environ if env is None else env
        token = env.get("MATRIX_ACCESS_TOKEN") or ""
        rooms = _csv(env.get("MATRIX_ROOM_ALLOW"))
        if not token or not rooms:
            return None
        return cls(
            homeserver=(env.get("MATRIX_HOMESERVER")
                        or DEFAULT_HOMESERVER).rstrip("/"),
            token=token,
            rooms=rooms,
            timeout_ms=int(env.get("MATRIX_SYNC_TIMEOUT_MS")
                           or DEFAULT_SYNC_TIMEOUT_MS),
        )


_THREAD_NS = uuid.UUID("6f1d3c52-9a0e-4c1b-8f3e-5a7d2b9c4e10")


def thread_of(room_id: str) -> str:
    """The thread id of a room: a uuid, stable for the room, so the server's
    session routes take it as they are and speech files under it."""
    return str(uuid.uuid5(_THREAD_NS, room_id))


def owner_of(env: dict | None = None) -> str:
    """Who `user` is in a room — whose words are never read out:
    `MATRIX_OWNER_ID`, else the first control id that is not the agent's."""
    env = os.environ if env is None else env
    if env.get("MATRIX_OWNER_ID"):
        return env["MATRIX_OWNER_ID"]
    sam = env.get("MATRIX_SAM_ID") or ""
    ids = [i.strip() for i in (env.get("MATRIX_CONTROL_IDS") or "").split(",")]
    return next((i for i in ids if i and i != sam), "")


def state_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME",
                               str(Path.home() / ".local" / "state")))
    d = base / "agent-media" / "matrix"
    d.mkdir(parents=True, exist_ok=True)
    return d


def mxc_to_http(homeserver: str, mxc: Optional[str]) -> Optional[str]:
    if not mxc or not mxc.startswith("mxc://"):
        return None
    rest = mxc[len("mxc://"):]
    if "/" not in rest:
        return None
    server, media_id = rest.split("/", 1)
    return f"{homeserver}/_matrix/client/v1/media/download/{server}/{media_id}"


def download(url: str, token: str, dest: Path, timeout: float = 60.0) -> bool:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            dest.write_bytes(resp.read())
        return dest.exists() and dest.stat().st_size > 0
    except (urllib.error.URLError, OSError) as e:
        log.warning("matrix: download failed (%s): %s", url, e)
        return False


def _fetch_json(req: urllib.request.Request, timeout: float) -> dict:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


class Sync:
    """The loop. `subscribe` before `start`; `poll_once` is one pass, for
    tests and for callers that drive the loop themselves."""

    def __init__(self, config: Config, *, state_path: Path | None = None,
                 fetch: Callable[[urllib.request.Request, float], dict] = _fetch_json):
        self.config = config
        self.state_path = state_path or (state_dir() / "sync.json")
        self._fetch = fetch
        self._subs: list[Subscriber] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = self._load()

    def subscribe(self, fn: Subscriber) -> None:
        self._subs.append(fn)

    # -- state (the same file the standalone intake always kept) --------

    def _load(self) -> dict:
        try:
            st = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            st = {}
        return {"next_batch": st.get("next_batch"),
                "seen": list(st.get("seen") or [])}

    def _save(self) -> None:
        try:
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._state))
            tmp.replace(self.state_path)
        except OSError as e:
            log.warning("matrix: state save failed: %s", e)

    # -- the loop -------------------------------------------------------

    def poll_once(self) -> None:
        """One `/sync`; raises on a network error (the loop backs off)."""
        c = self.config
        params = {"timeout": str(c.timeout_ms)}
        if self._state["next_batch"]:
            params["since"] = self._state["next_batch"]
        req = urllib.request.Request(
            f"{c.homeserver}/_matrix/client/v3/sync?"
            + urllib.parse.urlencode(params),
            headers={"Authorization": f"Bearer {c.token}"},
        )
        data = self._fetch(req, c.timeout_ms / 1000 + 30)
        self._state["next_batch"] = data.get("next_batch")
        seen = self._state["seen"]
        joined = (data.get("rooms") or {}).get("join") or {}
        for room_id, room in joined.items():
            if room_id not in c.rooms:
                continue
            for ev in (room.get("timeline") or {}).get("events") or []:
                ev_id = ev.get("event_id")
                if ev_id and ev_id in seen:
                    continue
                for fn in self._subs:
                    try:
                        fn(room_id, ev)
                    except Exception as e:  # noqa: BLE001 — one reader can't stop the rest
                        log.warning("matrix: subscriber failed: %s", e)
                if ev_id:
                    seen.append(ev_id)
        del seen[:-_SEEN_KEEP]
        self._save()

    def run(self) -> None:
        log.info("matrix: sync starting (homeserver=%s, rooms=%s)",
                 self.config.homeserver, ",".join(sorted(self.config.rooms)))
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self.poll_once()
                backoff = 1.0
            except (urllib.error.URLError, socket.timeout, OSError) as e:
                log.warning("matrix: sync failed: %s; retry in %.1fs", e, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
            except Exception as e:  # noqa: BLE001
                log.exception("matrix: unexpected sync error: %s", e)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
        log.info("matrix: sync stopped")

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self.run, name="matrix-sync",
                                            daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def wait_forever(sync: Sync, should_run: Callable[[], bool]) -> None:
    """Run `sync` on this thread until `should_run()` turns false."""
    sync.start()
    while should_run():
        time.sleep(0.5)
    sync.stop()
