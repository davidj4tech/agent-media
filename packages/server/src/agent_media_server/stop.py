"""`POST /session/stop` — the thing that is happening (server-contract.md §12).

Stop does one thing per press. With `speech: "auto"` (the default, what the
app's `onCancel` sends):

* the session is **working** → interrupt the turn, through the driver that owns
  it (driver/): the `interrupt` control request for a headless session,
  Escape for a pane (and only while it is working). Speech is left alone:
  what is playing or queued was said before the stop and is still true.
* not working, and **this thread's speech** is playing or paused → stop the
  clip (`SinkSpeech().stop`, what `media stop` does).
* neither → nothing.

`speech: "silence"` (the app's second press within 5 s) also stops this
thread's speech whatever the turn is doing. Another thread's speech is never
touched, and a session stopped on a question is never pressed — that answer
belongs to `/session/answer`.

**Not built: the per-session speech marker** (§12 "The cutoff", core work in
`intake/submit.py`). Without it this cannot drop this thread's speech that is
*submitted after* the stop, nor this thread's replies already *queued* behind
the one playing; only the current clip is stopped, and only when it is this
thread's. So `cutoff` is always `null` for now, and a reply the interrupted
turn had already handed to the Stop hook will still be spoken.
"""

from __future__ import annotations

import sys

from . import auth, driver, sessions, speech

_MODES = ("auto", "silence")


def session_stop(session: str, mode: str, bearer: str) -> tuple[bool, dict]:
    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    mode = (mode or "auto").strip().lower()
    if mode not in _MODES:
        return False, {"error": "speech must be auto or silence", "status": 400}
    drv = driver.for_session(session)
    before = drv.state(session)

    # Whose speech is heard now. Read before interrupting: an interrupt ends
    # the turn, and a reply it had finished may start speaking a moment later
    # — that one is the cutoff's (not built), not this press's.
    snap = speech.current_state()
    live_speech = bool(snap.get("speaking") or snap.get("paused"))
    this_thread = live_speech and str(snap.get("session") or "") == session

    interrupted, why = False, None
    if before["state"] == "working":
        ok, r = drv.interrupt(session)
        if not ok:
            # 504: the keys went nowhere (a pane still working after Escape).
            return False, {**r, "session": session}
        interrupted, why = bool(r.get("interrupted")), r.get("why")
    elif not before["live"]:
        why = "not live"
    elif before["state"] == "approval":
        why = "waiting on a question"
    else:
        why = "not working"

    stop_it = this_thread and (mode == "silence" or before["state"] != "working")
    if stop_it:
        err = speech.stop_speech()
        if err:
            print(f"stop: could not stop speech for {session[:8]} ({err})", file=sys.stderr)
        said = "stopped"
    elif this_thread:
        said = "left"
    elif live_speech:
        said = "other_thread"
    else:
        said = "idle"
    did = ("both" if interrupted and stop_it else "interrupted" if interrupted
           else "silenced" if stop_it else "nothing")
    after = drv.state(session)
    return True, {"session": session, "did": did, "interrupted": interrupted,
                  "why": None if interrupted else why, "speech": said,
                  "cutoff": None, "state": after["state"]}
