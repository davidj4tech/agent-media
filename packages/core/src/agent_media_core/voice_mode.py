"""Voice mode: a thread in the app's hands-free loop (David, 28–29 Sep 2026).

The phone's `/reply` carries `voice: true` while the loop is on; the server
marks the session, and the prompt hook the same words set off
(intake/heard.py) adds a note asking for a short, spoken answer. The note is
context, not text: nothing is added to the message or the transcript. A
reply without the flag clears the mark, and a mark goes stale after
TTL_S, so a loop left on does not shape tomorrow's answers.

The nudge (David, 22 Oct 2026): when the loop hears nothing after a reply,
the app sends one `/reply` with `nudge: true` and no words, once per reply.
The session is asked again — `nudge_text()`, a block the transcript and the
shelf drop like a system reminder (intake/_text.py), so the listener sees
only the agent's new line. After a question it asks it once more, shorter
and simpler; after anything else it offers one useful next step as a yes/no
question, or answers `QUIET` when there is none, which is never spoken
(`is_quiet`) and never shown.

The quick model (David, 22 Oct 2026): while the loop is on, a thread's
spoken replies come from a faster model (`VOICE_MODELS`, per harness: Claude
→ `sonnet`), and the heavy work it hands to background agents still runs on
the strong one (`SUBAGENT_MODELS`: `opus`) — the note tells it to pass that
model when it spawns one. The server switches the thread on the way in and
back to what it ran before on the way out (agent_media_server/voice_model.py),
always between turns; the switch is recorded here (`switched`) so the prompt
hook knows to add the subagent line. MEDIA_VOICE_MODEL=0 turns it off;
MEDIA_VOICE_MODEL_<HARNESS> / MEDIA_VOICE_SUBAGENT_MODEL_<HARNESS> name other
models.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

TTL_S = 900.0

NOTE = (
    "The listener is talking to you hands-free: your reply is read aloud and "
    "they answer by voice. Answer in one to three short spoken sentences, "
    "plain words, no lists, tables, code or links; leave the detail in the "
    "thread for later. If a decision is needed, end with it as one question "
    "(yes or no, or two options). If they say \"more\" or \"go on\", give the "
    "next part. Before anything hard to undo (a push, a delete, a send), ask "
    "aloud and wait for a yes. For a long job, say it has started and that "
    "you'll tell them when it's done."
)


#: The tag a nudge rides in. Stripped wherever the listener's words are read
#: (intake/_text.py `_SYSTEM_BLOCKS`): nobody said it.
NUDGE_TAG = "voice-nudge"

#: The answer that means "nothing worth asking": not spoken, not shown.
QUIET = "[[quiet]]"

NUDGE = (
    "The listener is hands-free and did not answer after your last reply "
    "(silence, nothing heard). This note is from the app, not from them. If "
    "your last reply ended with a question, ask it once more in one short "
    "sentence, simpler, ideally yes or no; do not repeat the reply. If it "
    "did not, offer the one most useful next step as a short yes-or-no "
    "question (for example \"Want me to install it now?\"); if there is "
    "genuinely no sensible next step, reply with exactly " + QUIET + " and "
    "nothing else. Do not start any work or use any tools for this; just ask."
)


#: The faster model a harness's thread talks on in voice mode. A harness not
#: listed keeps its model (Claude only for now).
VOICE_MODELS = {"claude": "sonnet"}

#: The model its background agents are told to run on meanwhile.
SUBAGENT_MODELS = {"claude": "opus"}

SUBAGENT_NOTE = (
    "Your spoken replies run on a faster model while the listener is hands-free. "
    "When you hand real work to a background agent (the Agent tool), pass "
    "model: \"{model}\" so it runs on the stronger model; keep quick answers "
    "to yourself."
)


def _enabled() -> bool:
    return os.environ.get("MEDIA_VOICE_MODEL", "1").strip().lower() not in ("0", "off", "false", "no")


def voice_model(agent: str) -> str:
    """The model `agent`'s thread switches to in voice mode, or "" (off, or
    a harness without one)."""
    if not _enabled():
        return ""
    agent = (agent or "").strip().lower()
    env = os.environ.get(f"MEDIA_VOICE_MODEL_{agent.upper()}", "").strip()
    return env or VOICE_MODELS.get(agent, "")


def subagent_model(agent: str) -> str:
    agent = (agent or "").strip().lower()
    env = os.environ.get(f"MEDIA_VOICE_SUBAGENT_MODEL_{agent.upper()}", "").strip()
    return env or SUBAGENT_MODELS.get(agent, "")


def _model_path(session: str) -> Path:
    return _dir() / (hashlib.sha1(session.encode("utf-8")).hexdigest() + ".model")


def record_switch(session: str, agent: str, prev: str, to: str, *, now: float | None = None) -> None:
    """`session` was switched from `prev` to `to` for voice mode."""
    session = (session or "").strip()
    if not session:
        return
    path = _model_path(session)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"session": session, "agent": agent, "prev": prev,
                                   "to": to, "at": time.time() if now is None else now}))
        tmp.replace(path)
    except OSError:
        pass


def switched(session: str) -> dict | None:
    """The switch voice mode made on `session` and has not undone:
    `{"session", "agent", "prev", "to", "at"}`, or None."""
    session = (session or "").strip()
    if not session:
        return None
    try:
        rec = json.loads(_model_path(session).read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("to") else None


def clear_switch(session: str) -> None:
    try:
        _model_path((session or "").strip()).unlink(missing_ok=True)
    except OSError:
        pass


def all_switched() -> list[dict]:
    """Every switch not yet undone."""
    out = []
    try:
        paths = list(_dir().glob("*.model"))
    except OSError:
        return out
    for p in paths:
        try:
            rec = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("session") and rec.get("to"):
            out.append(rec)
    return out


def note(session: str) -> str:
    """The prompt hook's note for `session`: NOTE while in voice mode, with
    the subagent line when its model was switched; "" otherwise."""
    if not active(session):
        return ""
    rec = switched(session)
    sub = subagent_model(str(rec.get("agent") or "")) if rec else ""
    return NOTE + ("\n\n" + SUBAGENT_NOTE.format(model=sub) if sub else "")


def nudge_text() -> str:
    """What a nudge puts into the session: the note in its own tag."""
    return f"<{NUDGE_TAG}>{NUDGE}</{NUDGE_TAG}>"


def is_quiet(text: str) -> bool:
    """A reply that is only the QUIET marker: the agent had nothing to ask."""
    return " ".join((text or "").split()).lower() == QUIET


def _dir() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return state / "agent-media" / "voice-mode"


def _path(session: str) -> Path:
    return _dir() / hashlib.sha1(session.encode("utf-8")).hexdigest()


def mark(session: str, on: bool, *, now: float | None = None) -> None:
    """The listener's latest words to `session` came from the loop (on) or not."""
    session = (session or "").strip()
    if not session:
        return
    path = _path(session)
    try:
        if not on:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"session": session, "at": time.time() if now is None else now}))
    except OSError:
        pass


def active(session: str, *, now: float | None = None) -> bool:
    """Is `session` in voice mode (marked within TTL_S)?"""
    session = (session or "").strip()
    if not session:
        return False
    try:
        rec = json.loads(_path(session).read_text())
    except (OSError, ValueError):
        return False
    at = rec.get("at") if isinstance(rec, dict) else None
    now = time.time() if now is None else now
    return isinstance(at, (int, float)) and now - at <= TTL_S
