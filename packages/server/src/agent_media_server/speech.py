"""What is being said, for the app's speech bar.

Moved out of the canvas (reply.py's `speech_now`, canvas.py's `/speech/ctl`
whitelist). The speech snapshot itself is still made by the canvas — it is
the producer of the SSE `state` frame — so the canvas hands in two callbacks
at startup (`set_speech`): one that returns that snapshot, and one that runs
a whitelisted speech transport verb. This package never imports `visual`.
"""

from __future__ import annotations

import time

from . import auth_abs, sessions, threads


# --- what is being said, for the app's mini player ------------------------------

#: `{session: (title, item, at)}` — a reply is polled every couple of seconds
#: for as long as it plays, and its title and library item do not change while
#: it does, so asking tmux and ABS once a minute is plenty.
_NOW_CACHE: dict[str, tuple[str, str | None, float]] = {}
_NOW_TTL_S = 60.0


def _session_title(session: str) -> str:
    for row in sessions.sessions_index():
        if row.get("session") == session:
            return str(row.get("title") or "")
    return ""


def speech_now(bearer: str, state: dict) -> tuple[bool, dict]:
    """`/speech/now`: what is being said right now, named for a person.

    The canvas's speech snapshot (`state`) says whether a voice is live and
    which session it belongs to; this adds what the app needs to show it
    anywhere — the conversation's title and its library item on the caller's
    own server, so a tap can open it. Gated like `/conversation`: titles and
    sentences are the conversation's, and the ABS bearer is the credential.
    """
    user, status = auth_abs.abs_identity(bearer)
    if not user:
        return False, auth_abs._identity_error(status)
    ok, why = auth_abs.may_reply(user)
    if not ok:
        return False, {"error": why, "status": 403}
    speaking = bool(state.get("speaking"))
    paused = bool(state.get("paused"))
    out = {"live": speaking or paused, "speaking": speaking, "paused": paused,
           "sentence": state.get("sentence") or "", "session": None,
           "title": "", "item": None,
           "pos": state.get("pos"), "dur": state.get("dur"),
           "speed": state.get("speed"), "muted": bool(state.get("muted"))}
    session = str(state.get("session") or "")
    if out["live"] and sessions._SESSION.fullmatch(session):
        now = time.time()
        hit = _NOW_CACHE.get(session)
        if not hit or now - hit[2] > _NOW_TTL_S:
            item, ready = threads.item_for_session(session, bearer)
            hit = (_session_title(session), item if ready else None, now)
            _NOW_CACHE[session] = hit
        out.update(session=session, title=hit[0], item=hit[1])
    return True, out
