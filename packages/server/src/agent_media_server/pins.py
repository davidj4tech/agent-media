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


def session_priority(session: str, flag, bearer: str, level=None) -> tuple[bool, dict]:
    """`POST /session/priority {"session", "level"}`: the thread's speech
    level — `interrupt`, `auto`, `normal` or `quiet`
    (`agent_media_core.speak_priority`). The older `{"priority": bool}` is
    auto / normal and still accepted. Refused exactly as `/session/pin` is;
    with neither given, auto."""
    from agent_media_core import speak_priority

    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    if level is None:
        if flag is None:
            flag = True
        if not isinstance(flag, bool):
            return False, {"error": "priority must be true or false", "status": 400}
        level = "auto" if flag else "normal"
    if level not in speak_priority.LEVELS:
        return False, {"error": "level must be interrupt, auto, normal or quiet",
                       "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if not (sessions.live_sessions().get(session) or sessions.session_exists(session)
            or sessions._folder_for_session(session)):
        return False, {"error": f"no such session {session[:8]}", "status": 404}
    try:
        speak_priority.set_level(session, level)
    except OSError as e:
        return False, {"error": f"could not save the level ({e})", "status": 500}
    return True, {"session": session, "level": level,
                  "priority": level in speak_priority.SPEAKS}


def speech_default(bearer: str, level=None) -> tuple[bool, dict]:
    """`GET /speech/default` and `POST /speech/default {"level"}`: the speech
    level of every thread with none of its own (`speak_priority.default_level`).
    The server's, so every device's. With `level` None, only read."""
    from agent_media_core import speak_priority

    if level is not None and level not in speak_priority.LEVELS:
        return False, {"error": "level must be interrupt, auto, normal or quiet",
                       "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if level is not None:
        try:
            speak_priority.set_default(level)
        except OSError as e:
            return False, {"error": f"could not save the default ({e})", "status": 500}
    return True, {"level": speak_priority.default_level()}
