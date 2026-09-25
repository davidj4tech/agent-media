"""What is being said, for the app's speech bar.

Moved out of the canvas (reply.py's `speech_now`, canvas.py's `/speech/ctl`
whitelist). The speech snapshot itself is still made by the canvas — it is
the producer of the SSE `state` frame — so the canvas hands in two callbacks
at startup (`set_speech`): one that returns that snapshot, and one that runs
a whitelisted speech transport verb. This package never imports `visual`.
"""

from __future__ import annotations

import sys
import time
from typing import Callable

from . import audio, auth, sessions, threads

#: What the app's speech player may do: the popup's listening keys — pause,
#: the sentence and paragraph steps, older/newer turn and replay, speed,
#: volume and a momentary mute — and "read from here": `goto-sentence` (jump
#: the reply being said to one of its sentences) and `replay-id` with a
#: `sentence` (play a recorded reply from one). The bearer is a listener's;
#: the popup's other keys (keep a pane muted, focus tmux, open URLs) are the
#: desk's.
_APP_SPEECH_ACTIONS = frozenset({
    "toggle", "skip-", "skip+", "para-", "para+", "jump-end",
    "prev", "replay", "replay-id", "speed-", "speed+", "speed0", "vol-", "vol+", "mute",
    "goto-sentence",
})

#: The highest sentence index a request may name. Far past any reply (the
#: canvas keeps 120 lines of one); only here so a number is a number.
MAX_SENTENCE = 9999

#: The canvas's speech snapshot (`canvas.speech_state`), and its runner for one
#: whitelisted speech verb (`action, arg -> what media said`). Handed in by
#: the canvas at import (`set_speech`); a server with no canvas has a speech
#: bar that is always quiet and controls that do nothing.
_STATE: Callable[[], dict] | None = None
_CTL: Callable[..., str] | None = None


def set_speech(state: Callable[[], dict] | None = None,
               ctl: Callable[..., str] | None = None) -> None:
    """Hand in the canvas's speech snapshot and its transport runner."""
    global _STATE, _CTL
    _STATE, _CTL = state, ctl


def current_state() -> dict:
    """The live speech snapshot, as the canvas's SSE `state` frame has it."""
    return _STATE() if _STATE is not None else {"kind": "state", "speaking": False}


def run_ctl(action: str, arg: int, sentence: int | None = None,
            session: str = "") -> str:
    """Run one speech verb already checked against `_APP_SPEECH_ACTIONS`.
    `sentence` is the index for `goto-sentence` and `replay-id`'s "from
    here"; it rides as the canvas's `sarg` (its `arg` is clamped 1-999, and
    sentence zero is the one most often asked for). `session` is `replay`'s
    thread, already checked, and rides the same way."""
    if _CTL is None:
        return ""
    if session:
        return _CTL(action, arg, session)
    if sentence is None:
        return _CTL(action, arg)
    return _CTL(action, arg, str(int(sentence)))


def sentence_arg(value) -> int | None:
    """A request's sentence index: a whole number 0..MAX_SENTENCE, else None.
    `True` is not 1 and "3" is not 3 — the app sends numbers."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_SENTENCE else None


def goto_refusal(session: str = "") -> str:
    """Why `goto-sentence` cannot run now, or "". It moves the reply being
    said; with none, `media skip --to` would bring back whichever reply the
    canvas last showed and jump into that — a tap on one message must never
    start another. `session`, when the app names it, must be the one heard."""
    st = current_state()
    if not (st.get("speaking") or st.get("paused")):
        return "nothing is being said"
    if session and st.get("session") and st.get("session") != session:
        return "that reply is no longer being said"
    return ""


def row_sentences(row_id: int) -> list[str] | None:
    """The sentences a replay of history row `row_id` can start at
    (`cli.replay_sentence_map`), [] when it can only play from the top, or
    None when there is no such spoken row."""
    from agent_media_core.cli import replay_sentence_map
    from agent_media_core.state.store import StateStore

    row = StateStore().history_row(int(row_id))
    if not row or row.get("sink") != "speech":
        return None
    return replay_sentence_map(row)


def stop_speech() -> str:
    """Stop the clip playing now, on the player it is playing on — what
    `media stop` does. "" when done, else why not. `/session/stop` calls this
    only after checking the clip is the thread's own (stop.py)."""
    try:
        from agent_media_core.cli import _active_speech_target
        from agent_media_core.sinks.speech import SinkSpeech, mark_speech_stopped

        mark_speech_stopped()
        SinkSpeech().stop(_active_speech_target())
        return ""
    except Exception as e:  # noqa: BLE001 — reported, never raised into a route
        return str(e) or type(e).__name__


# --- the per-session speech marker (/session/stop, server-contract.md §12) -----
#
# Thin seams over core's marker (`intake/submit.py`), so stop.py reads as the
# table in §12 and the tests can record what was cut without a state dir.


def cut_session(session: str, mode: str, at: float) -> float | None:
    """Skip this thread's speech from `at`: `after` (what it says next, until
    the listener's next turn) or `all` (what it has not been heard saying)."""
    from agent_media_core.intake.submit import request_session_speech_cut

    return request_session_speech_cut(session, mode, at)


def reply_read(session: str, keep: bool = False) -> None:
    """The listener replied to this thread: stop reading its last reply at the
    end of the sentence, or with `keep`, leave it and tell the prompt hook to."""
    try:
        from agent_media_core.intake.submit import session_reply_read

        session_reply_read(session, keep=keep)
    except Exception as e:  # noqa: BLE001 — the reply goes in either way
        print(f"reply: could not mark the reply read ({e})", file=sys.stderr)


def has_cutoff(session: str) -> bool:
    """Whether an `after` cutoff already stands on this thread."""
    from agent_media_core.intake.submit import session_speech_cut

    return "after" in session_speech_cut(session)


def end_cutoff(session: str) -> None:
    """Lift this thread's `after` cutoff (the listener spoke, or nothing was
    interrupted after all)."""
    from agent_media_core.intake.submit import end_session_speech_cut

    end_session_speech_cut(session)


def waiting_for(session: str, at: float) -> list[dict]:
    """This thread's replies waiting for the voice, submitted by `at` — what an
    `all` cut at `at` drops (replies still rendering are not listed)."""
    from agent_media_core.intake.submit import speech_queue

    return [q for q in speech_queue()
            if q.get("session") == session and float(q.get("at") or 0) <= at]


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
    # Which turn the player is on, keyed the way the log's lines are
    # (`{"at", "id"}`). The transcript can then match `pos` to a line it
    # already holds — which is the whole point of a line carrying its
    # timeline without being live: the bold survives losing the live row.
    if out["live"] and isinstance(state.get("turn"), dict):
        out["turn"] = state["turn"]
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
