"""`POST /voice/check` — did the phone hear what was meant? (David, 24 Oct 2026)

Hands-free with the screen off and an earbud in, nobody reads the words in
the box before the countdown sends them. So the app asks here first: a fast
model gets the words the recogniser heard (and how sure it was), with the
line they answer, and says whether they make sense as heard (`sure`) and, in
one short sentence, what the listener meant (`gist`). When it is not sure the
app asks "Did you mean …?" and waits for a yes before it sends.

Asked only when unsure: low recogniser confidence alone makes it unsure, and
so does a model that finds the words garbled, ambiguous or likely misheard.
Anything that goes wrong — no gateway, a timeout, an answer that is not the
JSON asked for, the check switched off — answers `sure: true`, so the loop
sends exactly as it did before there was a check.

With `say` (the app's own words, in its language, with `{gist}` in them) the
question is spoken in the thread the way a question card's next part is
(hook_claude_code.speak_ask_part: submit_event with the thread's voice and
place), and kept out of speech history (`private`).

Config (env / ~/.config/agent-media.env):
  MEDIA_VOICE_CHECK           "0" turns it off (always sure)
  MEDIA_VOICE_CHECK_MODEL     the model (default MEDIA_FOLLOWUP_MODEL, then
                              the summary's); the gateway is the summary's
                              (intake/_summary.py, MEDIA_SUMMARY_*). It must
                              answer in a second or two: on red5's gateway
                              gemini-3-flash takes 0.5–2.8 s, claude-haiku-4-5
                              about 15 s (24 Oct 2026)
  MEDIA_VOICE_CHECK_TIMEOUT   seconds the whole check may take (default 3)
  MEDIA_VOICE_CHECK_MIN_CONFIDENCE  recogniser confidence under this is
                              unsure (default 0.5); 0 or less means unknown
"""

from __future__ import annotations

import json
import os
import re
import threading

from . import auth, sessions

DEFAULT_TIMEOUT_S = 3.0
DEFAULT_MIN_CONFIDENCE = 0.5
#: Characters of the heard words, and of the line they answer, sent to the model.
MAX_WORDS = 1200
MAX_CONTEXT = 1500
#: The gist the app reads out: a sentence, not a paragraph.
MAX_GIST = 240

PROMPT = (
    "You check a speech recogniser's transcript before it is sent to an AI "
    "assistant. The listener spoke with the screen off and cannot read what "
    "was heard. You are given what the assistant last said (it may be empty) "
    "and the words the recogniser heard. Decide whether the words make sense "
    "as heard, as a reply in that conversation. They are NOT sure when they "
    "look garbled, misheard (wrong words that sound alike), cut off mid-thought, "
    "or so ambiguous the assistant could easily take them the wrong way. Plain "
    "short answers such as yes, no, go ahead, or a number are sure when they "
    "fit what the assistant said. Then restate what the listener most likely "
    "meant in ONE short spoken sentence, in the same language as their words, "
    "phrased as the request itself (for example 'restart the music server', "
    "'yes, merge it'), no quotes, no markdown. Answer with JSON only: "
    "{\"sure\": true|false, \"gist\": \"...\"}."
)


def _enabled() -> bool:
    return os.environ.get("MEDIA_VOICE_CHECK", "1") != "0"


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _model() -> str | None:
    return (os.environ.get("MEDIA_VOICE_CHECK_MODEL")
            or os.environ.get("MEDIA_FOLLOWUP_MODEL") or None)


def _ask_model(words: str, context: str, timeout: float) -> dict | None:
    """`{"sure", "gist"}` from the model, or None: no answer within `timeout`
    (a hard deadline: the request runs on its own thread), or none usable."""
    from agent_media_core.intake import _summary

    user = (f"The assistant last said:\n<said>{context}</said>\n\n"
            f"The recogniser heard:\n<heard>{words}</heard>")
    out: list[str | None] = []

    def run() -> None:
        try:
            # urlopen takes a float; the join below is the real deadline.
            out.append(_summary._chat(PROMPT, user, timeout=timeout,  # type: ignore[arg-type]
                                      model=_model()))
        except Exception:  # noqa: BLE001 — a failed check is a sure one
            out.append(None)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return parse(out[0]) if out else None


def parse(text: str | None) -> dict | None:
    """The model's JSON, leniently: in a code fence or with words around it."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        got = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(got, dict) or not isinstance(got.get("sure"), bool):
        return None
    gist = " ".join(str(got.get("gist") or "").split()).strip(" \"'“”")
    if len(gist) > MAX_GIST:
        gist = gist[:MAX_GIST].rsplit(" ", 1)[0] + "…"
    return {"sure": got["sure"], "gist": gist}


def check(words: str, context: str = "", confidence: float | None = None) -> dict:
    """`{"sure", "gist", "why"?}` for these words; never raises."""
    words = " ".join(str(words or "").split())[:MAX_WORDS]
    if not words:
        return {"sure": True, "gist": "", "why": "no_words"}
    if not _enabled():
        return {"sure": True, "gist": "", "why": "off"}
    got = _ask_model(words, " ".join(str(context or "").split())[-MAX_CONTEXT:],
                     _float_env("MEDIA_VOICE_CHECK_TIMEOUT", DEFAULT_TIMEOUT_S))
    if not got:
        return {"sure": True, "gist": "", "why": "no_answer"}
    floor = _float_env("MEDIA_VOICE_CHECK_MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE)
    low = confidence is not None and 0 < confidence < floor
    if not got["gist"]:
        # Nothing to read back: asking "did you mean …?" of nothing helps nobody.
        return {"sure": True, "gist": "", "why": "no_gist"}
    if low and got["sure"]:
        return {"sure": False, "gist": got["gist"], "why": "low_confidence"}
    return {"sure": got["sure"], "gist": got["gist"]}


def speak(session: str, text: str) -> None:
    """Say `text` in `session`'s voice and place, as a question card's part is,
    and keep it out of speech history. Off the caller's thread: submit waits
    on playback."""
    from agent_media_core.intake.submit import submit_event
    from agent_media_core.state.store import StateStore
    from agent_media_core.types import Event, Priority, Source

    event = Event(text=text, source=Source.CLAUDE_CODE, priority=Priority.NORMAL,
                  metadata={"kind": "notif", "voice_from": "session", "session": session,
                            "private": True, "voice_check": True})

    def run() -> None:
        try:
            submit_event(event, state=StateStore())
        except Exception:  # noqa: BLE001 — unspoken, it is still on screen
            pass

    threading.Thread(target=run, daemon=True).start()


def post(body: dict, bearer: str) -> tuple[bool, dict]:
    """POST /voice/check {"text", "session"?, "context"?, "confidence"?, "say"?}."""
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    body = body if isinstance(body, dict) else {}
    text = body.get("text")
    if not isinstance(text, str):
        return False, {"error": "text is required", "status": 400}
    session = str(body.get("session") or "").strip()
    if session and not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    conf = body.get("confidence")
    confidence = float(conf) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else None
    context = body.get("context") if isinstance(body.get("context"), str) else ""
    got = check(text, context, confidence)
    say = body.get("say")
    spoken = False
    if not got["sure"] and session and isinstance(say, str) and "{gist}" in say:
        speak(session, say.replace("{gist}", got["gist"])[:MAX_GIST + 200])
        spoken = True
    return True, {**got, "spoken": spoken}
