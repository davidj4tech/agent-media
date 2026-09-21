"""Archived threads: a flag per session, kept by this server.

Archiving used to be an Audiobookshelf tag on the conversation's item, so it
lived and died with the library (server-contract.md §16 listed it as a gap).
It is the app's thread list that needs it, and the list is `/targets`, which
knows sessions, not items — so the flag is kept here, by session id.

What it means, and what it does not:

* An archived thread is still listed. `/targets` and `/conversations` rows
  carry `archived: true` and the app files them under "Archived"; the server
  never drops them, so un-archiving from that section needs nothing but the
  row the app already has.
* Archiving does not end anything. A live session stays live; "End & archive"
  in the app is two requests, `/session/close` and `/session/archive`.
* Talking to a thread un-archives it: a reply or a routed ask that reaches a
  session clears its flag (`send.deliver`), because a thread you are talking
  to is not archived. A branch does not: it opens a new thread and leaves the
  one it quoted where it was.

Stored in `<state_dir>/archived.json` as `{"<session>": <archived at, epoch>}`.
Written whole, atomically (temp file + rename), and read-modify-written under
both a thread lock and an flock on a sibling lock file, so two handler
threads — or the canvas and a CLI — cannot lose each other's change.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import auth, sessions

_LOCK = threading.Lock()
# Parsed archived.json by (path, mtime_ns, size): `/targets` reads the flags
# on every open, and nearly always nothing has changed. The path is in the key
# because the state dir moves (XDG_STATE_HOME) — in tests, every time.
_CACHE: tuple[tuple | None, dict] = (None, {})


def _path() -> Path:
    from agent_media_core._paths import state_dir

    return state_dir() / "archived.json"


def _read() -> dict[str, float]:
    global _CACHE
    path = _path()
    try:
        st = path.stat()
    except OSError:
        return {}
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _CACHE[0] == key:
        return _CACHE[1]
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    flags = {str(k): float(v or 0) for k, v in data.items()
             if isinstance(v, (int, float)) and not isinstance(v, bool)}
    _CACHE = (key, flags)
    return flags


def archived() -> dict[str, float]:
    """`{session: archived at}` for every archived thread. A copy."""
    with _LOCK:
        return dict(_read())


def is_archived(session: str) -> bool:
    return session in archived()


def set_archived(session: str, flag: bool) -> bool:
    """Archive or un-archive `session`. True when that changed anything.

    Un-archiving a thread that is not archived writes nothing, which is what
    keeps `send.deliver` calling this on every reply cheap.
    """
    from agent_media_core import _lock as fcntl

    path = _path()
    with _LOCK:
        if (session in _read()) == flag:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "a") as lk:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
            try:
                # Re-read under the file lock: another process may have
                # written since the check above.
                flags = dict(_read())
                if (session in flags) == flag:
                    return False
                if flag:
                    flags[session] = round(time.time(), 3)
                else:
                    flags.pop(session, None)
                tmp = path.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
                tmp.write_text(json.dumps(flags, indent=0, sort_keys=True))
                tmp.replace(path)
            finally:
                fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
    return True


def unarchive_quietly(session: str) -> None:
    """Clear the flag because the thread was just talked to. Never raises: the
    message already reached the session, and a flag that could not be cleared
    is not worth failing the send over."""
    try:
        set_archived(session, False)
    except Exception:  # noqa: BLE001
        pass


def session_archive(session: str, flag, bearer: str) -> tuple[bool, dict]:
    """`POST /session/archive {"session", "archived"}`. Gated like `/session/close`.

    `archived` defaults to true (archive); anything but a JSON boolean is 400.
    A session no harness has, that is not running and not on the shelf, is
    404 — the same test `/reply {session}` makes before typing.
    """
    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    if flag is None:
        flag = True
    if not isinstance(flag, bool):
        return False, {"error": "archived must be true or false", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if not (sessions.live_sessions().get(session) or sessions.session_exists(session)
            or sessions._folder_for_session(session)):
        return False, {"error": f"no such session {session[:8]}", "status": 404}
    try:
        set_archived(session, flag)
    except OSError as e:
        return False, {"error": f"could not save the flag ({e})", "status": 500}
    return True, {"session": session, "archived": flag}
