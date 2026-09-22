"""`POST /session/stop` — the thing that is happening (server-contract.md §12).

Stop does one thing per press. With `speech: "auto"` (the default, what the
app's `onCancel` sends):

* the session is **working** → interrupt the turn, through the driver that owns
  it (driver/): the `interrupt` control request for a headless session,
  Escape for a pane (and only while it is working). Speech is left alone:
  what is playing or queued was said before the stop and is still true. What
  the interrupted turn says *after* the press is not: the `after` cutoff.
* not working, and **this thread's speech** is playing or paused → stop it:
  the clip (`SinkSpeech().stop`, what `media stop` does) and, through the
  `all` cut, the rest of that reply and everything this thread has queued.
* neither → nothing.

`speech: "silence"` (the app's second press within 5 s) also stops this
thread's speech whatever the turn is doing. Another thread's speech is never
touched, and a session stopped on a question is never pressed — that answer
belongs to `/session/answer`.

**The per-session speech marker** is core's (`request_session_speech_cut` in
`intake/submit.py`): one marker per Claude session with two modes. `after`
skips this thread's replies submitted after the press, until the listener's
next turn; `all` skips every one of its replies submitted up to the press
that has not been heard. A skipped reply is still archived, marked flushed.
"""

from __future__ import annotations

import sys
import time

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
    # The stop time. Both cuts are stamped with the press, not with whenever
    # the interrupt finished landing.
    pressed = time.time()

    # Whose speech is heard now. Read before interrupting: an interrupt ends
    # the turn, and a reply it had finished may start speaking a moment later
    # — that one is the cutoff's, not this press's.
    snap = speech.current_state()
    live_speech = bool(snap.get("speaking") or snap.get("paused"))
    this_thread = live_speech and str(snap.get("session") or "") == session

    interrupted, why, cutoff = False, None, None
    if before["state"] == "working":
        # The cutoff goes down before the keys, so a reply the turn finishes
        # while they land is already behind it — and comes up again if nothing
        # was interrupted, since that turn's reply is then still worth hearing.
        had_cutoff = speech.has_cutoff(session)
        cutoff = speech.cut_session(session, "after", pressed)
        ok, r = drv.interrupt(session)
        interrupted = bool(ok and r.get("interrupted"))
        if not interrupted:
            if not had_cutoff:
                speech.end_cutoff(session)
            cutoff = None
        if not ok:
            # 504: the keys went nowhere (a pane still working after Escape).
            return False, {**r, "session": session}
        why = r.get("why")
    elif not before["live"]:
        why = "not live"
    elif before["state"] == "approval":
        why = "waiting on a question"
    else:
        why = "not working"

    # This thread's speech, stopped: the clip it is on (only when it is the
    # one heard) and everything it has waiting. `silence` drops what is
    # waiting even while another thread is the one heard.
    stop_it = mode == "silence" or (this_thread and before["state"] != "working")
    flushed = 0
    if stop_it:
        flushed = len(speech.waiting_for(session, pressed))
        speech.cut_session(session, "all", pressed)
        if this_thread:
            err = speech.stop_speech()
            if err:
                print(f"stop: could not stop speech for {session[:8]} ({err})",
                      file=sys.stderr)
    silenced = stop_it and (this_thread or flushed > 0)
    if silenced:
        said = "stopped"
    elif this_thread:
        said = "left"
    elif live_speech:
        said = "other_thread"
    else:
        said = "idle"
    did = ("both" if interrupted and silenced else "interrupted" if interrupted
           else "silenced" if silenced else "nothing")
    after = drv.state(session)
    return True, {"session": session, "did": did, "interrupted": interrupted,
                  "why": None if interrupted else why, "speech": said,
                  "cutoff": cutoff, "flushed": flushed, "state": after["state"]}
