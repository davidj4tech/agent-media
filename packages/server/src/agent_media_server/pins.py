"""Keep-open pins: sessions the idle reaper must never close.

`POST /session/pin {"session", "pinned": bool}` sets one; `/targets` and
`/conversations` rows carry `pinned`. A pin is about the reaper only — it does
not stop you closing the session yourself, and it survives the session ending
(pin a thread, close it, resume it next week: still pinned).

Stored in `<state_dir>/pinned.json` as `{"<session>": <pinned at, epoch>}`
(`_jsonmap.JsonMap`).
"""

from __future__ import annotations

import time

from . import auth, sessions
from ._jsonmap import JsonMap

PINS = JsonMap("pinned.json")


def pinned() -> dict[str, float]:
    return {k: v for k, v in PINS.all().items() if isinstance(v, (int, float))}


def is_pinned(session: str) -> bool:
    return session in pinned()


def set_pinned(session: str, flag: bool) -> bool:
    """Pin or unpin. True when that changed anything."""
    if flag:
        def fn(rows: dict) -> bool:
            if session in rows:
                return False
            rows[session] = round(time.time(), 3)
            return True
        return PINS.update(fn)
    return PINS.drop(session)


def session_pin(session: str, flag, bearer: str) -> tuple[bool, dict]:
    """`POST /session/pin {"session", "pinned"}`. Gated like `/session/archive`,
    and refused the same ways: 400 for a bad id or a non-boolean, 404 for a
    session nothing knows. `pinned` defaults to true."""
    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    if flag is None:
        flag = True
    if not isinstance(flag, bool):
        return False, {"error": "pinned must be true or false", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if not (sessions.live_sessions().get(session) or sessions.session_exists(session)
            or sessions._folder_for_session(session)):
        return False, {"error": f"no such session {session[:8]}", "status": 404}
    try:
        set_pinned(session, flag)
    except OSError as e:
        return False, {"error": f"could not save the pin ({e})", "status": 500}
    return True, {"session": session, "pinned": flag}


def session_priority(session: str, flag, bearer: str) -> tuple[bool, dict]:
    """`POST /session/priority {"session", "priority"}`: mark a thread
    always-speak — its replies are never held by the desk toast or silenced
    by a pane mute (`agent_media_core.speak_priority`). Refused exactly as
    `/session/pin` is; `priority` defaults to true."""
    from agent_media_core import speak_priority

    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    if flag is None:
        flag = True
    if not isinstance(flag, bool):
        return False, {"error": "priority must be true or false", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if not (sessions.live_sessions().get(session) or sessions.session_exists(session)
            or sessions._folder_for_session(session)):
        return False, {"error": f"no such session {session[:8]}", "status": 404}
    try:
        speak_priority.set_priority(session, flag)
    except OSError as e:
        return False, {"error": f"could not save the priority ({e})", "status": 500}
    return True, {"session": session, "priority": flag}
