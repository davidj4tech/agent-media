"""What is being said, for the app's speech bar.

Moved out of the canvas (reply.py's `speech_now`, canvas.py's `/speech/ctl`
whitelist). The speech snapshot itself is still made by the canvas — it is
the producer of the SSE `state` frame — so the canvas hands in two callbacks
at startup (`set_speech`): one that returns that snapshot, and one that runs
a whitelisted speech transport verb. This package never imports `visual`.
"""

from __future__ import annotations

import time
from typing import Callable

from . import audio, auth, sessions, threads

#: What the app's speech player may do: the popup's listening keys — pause,
#: the sentence and paragraph steps, older/newer turn and replay, speed,
#: volume and a momentary mute. The bearer is a listener's; the popup's other
#: keys (keep a pane muted, focus tmux, open URLs) are the desk's.
_APP_SPEECH_ACTIONS = frozenset({
    "toggle", "skip-", "skip+", "para-", "para+", "jump-end",
    "prev", "replay", "replay-id", "speed-", "speed+", "speed0", "vol-", "vol+", "mute",
})

#: The canvas's speech snapshot (`canvas.speech_state`), and its runner for one
#: whitelisted speech verb (`action, arg -> what media said`). Handed in by
#: the canvas at import (`set_speech`); a server with no canvas has a speech
#: bar that is always quiet and controls that do nothing.
_STATE: Callable[[], dict] | None = None
_CTL: Callable[[str, int], str] | None = None


def set_speech(state: Callable[[], dict] | None = None,
               ctl: Callable[[str, int], str] | None = None) -> None:
    """Hand in the canvas's speech snapshot and its transport runner."""
    global _STATE, _CTL
    _STATE, _CTL = state, ctl


def current_state() -> dict:
    """The live speech snapshot, as the canvas's SSE `state` frame has it."""
    return _STATE() if _STATE is not None else {"kind": "state", "speaking": False}


def run_ctl(action: str, arg: int) -> str:
    """Run one speech verb already checked against `_APP_SPEECH_ACTIONS`."""
    return _CTL(action, arg) if _CTL is not None else ""


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


#: `{session: (title, at)}` for the waiting replies: same TTL, no ABS call.
_TITLE_CACHE: dict[str, tuple[str, float]] = {}


def _queued_row(q: dict) -> dict:
    """One waiting reply, as `/speech/now` `queued` lists it."""
    session = str(q.get("session") or "")
    title = ""
    if sessions._SESSION.fullmatch(session):
        now = time.time()
        hit = _TITLE_CACHE.get(session)
        if not hit or now - hit[1] > _NOW_TTL_S:
            hit = (_session_title(session), now)
            _TITLE_CACHE[session] = hit
        title = hit[0]
    else:
        session = ""
    try:
        at = float(q.get("at") or 0) or None
    except (TypeError, ValueError):
        at = None
    return {"session": session or None, "title": title,
            "urgent": bool(q.get("urgent")), "at": at}


def speech_now(bearer: str, state: dict) -> tuple[bool, dict]:
    """`/speech/now`: what is being said right now, named for a person.

    The canvas's speech snapshot (`state`) says whether a voice is live and
    which session it belongs to; this adds what the app needs to show it
    anywhere — the conversation's title and its library item on the caller's
    own server, so a tap can open it. Gated like `/conversation`: titles and
    sentences are the conversation's, and the ABS bearer is the credential.
    """
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    speaking = bool(state.get("speaking"))
    paused = bool(state.get("paused"))
    out = {"live": speaking or paused, "speaking": speaking, "paused": paused,
           "sentence": state.get("sentence") or "", "session": None,
           "title": "", "item": None,
           "pos": state.get("pos"), "dur": state.get("dur"),
           "speed": state.get("speed"), "muted": bool(state.get("muted"))}
    # Where that voice is (or the next one will be): the bar shows it, and
    # the picker behind it is /audio/targets.
    out["target"] = state.get("target") or audio.speech_target_now(out["live"])
    # A recorded reply played again (the bar's replay, ▶ on a bubble): what is
    # heard is that reply, under its own session, and not a new one.
    out["replay"] = bool(out["live"] and state.get("replay"))
    # Replies said but not heard yet, because the voice is busy. The app's
    # "New reply waiting". Named, like the live one, but title only: the
    # library item is looked up when it plays.
    out["queued"] = [_queued_row(q) for q in state.get("queued") or []
                     if isinstance(q, dict)]
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
