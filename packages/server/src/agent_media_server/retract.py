"""`POST /session/retract` — take back your last message (server-contract.md §6.19).

The app's tap on your own latest message: **Cancel** takes it back, **Edit**
takes it back and puts its words in the reply box. Either way it is taken back
here, the same way:

* the session is **working** → the turn is interrupted through its driver,
  and the messages queued behind it go too (a headless session's
  `cancel_queued`; a pane's queue is taken back into the composer and
  cleared first, since Escape alone would run it next). Speech gets the
  stop's `after` cutoff: what the interrupted turn says after the press is
  not worth hearing.
* not working → nothing to stop; the message is only marked.

The message is **marked** (`retracted: true` on it in `/conversation/log` and
the thread stream, matched by its id, or by its words when the app had no id
for it yet) and the **next** message sent to the session opens with a
sentence saying so (`NOTE`), since the agent may already have read it — the
original stays in the transcript, struck through on the phone. That sentence
is taken off the bubble again when the thread is read (`strip_note`).

Stored in `<state_dir>/retracted.json` as `{"<session>": {"items": [{"id",
"text", "at"}], "note": "<the words, when a note is owed>"}}`
(`_jsonmap.JsonMap`); a session keeps its last `KEEP` items.
"""

from __future__ import annotations

import re
import time

from . import auth, driver, sessions, speech
from ._jsonmap import JsonMap

STORE = JsonMap("retracted.json")
KEEP = 50
#: How much of the taken-back words the note quotes.
QUOTE_MAX = 80

_NOTE_RE = re.compile(r"^\(I took back my last message(?:, “[^”]*”)? — disregard it\.\)\s*")


def note(words: str) -> str:
    w = " ".join((words or "").replace("“", '"').replace("”", '"').split())
    if len(w) > QUOTE_MAX:
        w = w[:QUOTE_MAX - 1].rstrip() + "…"
    return f"(I took back my last message, “{w}” — disregard it.)" if w \
        else "(I took back my last message — disregard it.)"


def strip_note(text: str) -> str:
    """The words of a message without the note a retraction put before them."""
    return _NOTE_RE.sub("", text, count=1) if text.startswith("(I took back") else text


def take_note(session: str) -> str:
    """The note owed to `session`'s next message, once: "" when none is."""
    row = STORE.get(session)
    if not isinstance(row, dict) or not row.get("note"):
        return ""
    owed = str(row["note"])

    def fn(rows: dict) -> bool:
        r = rows.get(session)
        if not isinstance(r, dict) or not r.get("note"):
            return False
        r.pop("note", None)
        return True
    STORE.update(fn)
    return note(owed)


def with_note(session: str, text: str) -> str:
    """`text` as the next message should say it: the owed note, then the words."""
    owed = take_note(session)
    return f"{owed}\n\n{text}" if owed else text


def mark(session: str, msgs: list[dict]) -> None:
    """`retracted: true` on the messages taken back, in place. By id, else the
    latest user message with those words sent before it was taken back; and
    the note off any user message that carried one."""
    for m in msgs:
        if m.get("role") != "user":
            continue
        parts = m.get("parts") or []
        if parts and parts[0].get("type") == "text" and (parts[0].get("text") or "").startswith("(I took back"):
            parts[0]["text"] = strip_note(parts[0]["text"])
    row = STORE.get(session)
    items = row.get("items") if isinstance(row, dict) else None
    if not items:
        return
    ids = {str(i.get("id")) for i in items if i.get("id")}
    for m in msgs:
        if m.get("role") == "user" and m.get("id") in ids:
            m["retracted"] = True
    for it in items:
        if it.get("id") or not it.get("text"):
            continue
        words = " ".join(str(it["text"]).split())
        for m in reversed(msgs):
            if m.get("role") != "user" or (m.get("at") or 0) > (it.get("at") or 0) + 1:
                continue
            parts = m.get("parts") or []
            if parts and " ".join(str(parts[0].get("text") or "").split()) == words:
                m["retracted"] = True
                break


def _record(session: str, mid: str, text: str, at: float) -> None:
    def fn(rows: dict) -> bool:
        r = rows.get(session) if isinstance(rows.get(session), dict) else {}
        items = [i for i in (r.get("items") or []) if isinstance(i, dict)]
        items.append({"id": mid, "text": text, "at": at})
        r["items"] = items[-KEEP:]
        r["note"] = text or " "
        rows[session] = r
        return True
    STORE.update(fn)


def session_retract(session: str, mid: str, text: str, bearer: str) -> tuple[bool, dict]:
    """`{"session", "id"?, "text"?}` → `{"session", "retracted": {"id", "text"},
    "interrupted", "why", "state"}`. `id` is the message's (a transcript uuid),
    absent for one the app has sent but not yet seen come back; `text` is its
    words, which the pane needs to clear its queue and the note quotes."""
    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    mid = (mid or "").strip()
    text = (text or "").strip()
    if not mid and not text:
        return False, {"error": "retract needs the message's id or its words", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if not (sessions.live_sessions().get(session) or sessions.session_exists(session)
            or driver.owned_headless(session)):
        return False, {"error": f"no such session {session[:8]}", "status": 404}
    drv = driver.for_session(session)
    before = drv.state(session)
    pressed = time.time()
    interrupted, why = False, None
    if before["state"] == "working":
        had_cutoff = speech.has_cutoff(session)
        speech.cut_session(session, "after", pressed)
        ok, r = drv.interrupt(session, drop_queued=True, words=text)
        interrupted = bool(ok and r.get("interrupted"))
        if not interrupted and not had_cutoff:
            speech.end_cutoff(session)
        why = r.get("why") if ok else r.get("error")
    elif before["state"] == "approval":
        why = "waiting on a question"
    # Marked even when the interrupt did not land: the message is taken back
    # all the same, and the next one says so.
    _record(session, mid, text, pressed)
    after = drv.state(session)
    return True, {"session": session, "retracted": {"id": mid or None, "text": text},
                  "interrupted": interrupted, "why": why, "state": after["state"]}
