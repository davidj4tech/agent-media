"""How much of the last reply the listener had heard when they answered.

A reply is read aloud a sentence at a time, and the listener often answers
before it ends — the rest is cut at the sentence (`session_reply_read`), or
plays on under Keep reading. Either way the agent's next reply was written as
if every word had landed: it builds on a point that was never spoken, or says
again what was. This tells it where the voice had got to.

What was *spoken* is known exactly — the live speech row's timeline, the same
one the phone's follow-along bolds by (`book_tracks._live_turn`). What was
*read* is not: the whole reply is on screen from the start, and the listener
may have read ahead or not looked. So the note says what was spoken and leaves
the rest to the agent's judgement.

A synchronous UserPromptSubmit hook (`media-hook-heard`), separate from the
async speech hook, because only a hook that holds the prompt can add to it;
it reads one row and prints, ~150 ms. It says nothing when there is nothing to
say — no reply of this session playing, the last sentence already under way,
a settings command, a harness notice — and never fails the prompt.
`MEDIA_HEARD_NOTE=0` turns it off.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Optional

#: How much of a sentence the note quotes — enough for the agent to find it in
#: its own reply, not a second copy of the reply.
_QUOTE = 70


def _quote(sentence: str) -> str:
    s = " ".join(str(sentence).split())
    return s if len(s) <= _QUOTE else s[:_QUOTE - 1].rstrip() + "…"


def position(turn: dict) -> Optional[int]:
    """The sentence being spoken in `turn` (a `_live_turn`), 0-based, or None.

    The timeline when there is one — `elapsed` less the playout delay against
    each sentence's start, as the phone's highlight reads it — else the index
    the sentence loop last wrote, which can trail the voice by a sentence."""
    sentences = turn.get("sentences") or []
    if not sentences:
        return None
    offsets = [float(x) for x in (turn.get("offsets") or [])]
    if offsets:
        at = float(turn.get("elapsed") or 0) - float(turn.get("delay") or 0)
        idx = 0
        for i, start in enumerate(offsets[:len(sentences)]):
            if start <= at:
                idx = i
        return idx
    try:
        idx = int(turn.get("sentence"))
    except (TypeError, ValueError):
        return None
    return max(0, min(idx, len(sentences) - 1))


def note(turn: Optional[dict]) -> str:
    """The context line for a reply sent while `turn` was being spoken, or ""."""
    if not turn or turn.get("listener"):
        return ""
    sentences = [str(s) for s in (turn.get("sentences") or [])]
    idx = position(turn)
    if idx is None or idx >= len(sentences) - 1:
        return ""                       # the last sentence: all but heard
    n = len(sentences)
    unheard = n - idx - 1
    paused = " (paused there)" if turn.get("paused") else ""
    return (
        "agent-media: your previous reply was still being read aloud when this "
        f"message was sent{paused}. The voice had reached sentence {idx + 1} "
        f"of {n} (“{_quote(sentences[idx])}”); the remaining "
        f"{unheard} sentence{'s' if unheard != 1 else ''}, from "
        f"“{_quote(sentences[idx + 1])}”, had not been spoken. The "
        "full text was on screen, so they may have read ahead, but don't "
        "assume it: if something unspoken bears on this message, say it "
        "briefly rather than building on it.")


def _worth_a_note(prompt: str) -> bool:
    """Words someone said: not a settings command, not a harness notice."""
    from .. import slash
    from ._text import strip_system_blocks

    cmd = slash.parse(prompt)
    if cmd is not None:
        return not slash.is_settings(cmd.get("name") or "")
    return bool(strip_system_blocks(prompt).strip())


def note_for(payload: dict) -> str:
    session = str(payload.get("session_id") or "")
    if not session or not _worth_a_note(str(payload.get("prompt") or "")):
        return ""
    from ..book_tracks import _live_turn

    return note(_live_turn(session))


def main() -> int:
    if os.environ.get("MEDIA_HEARD_NOTE", "1") == "0":
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        text = note_for(payload) if isinstance(payload, dict) else ""
    except Exception:  # noqa: BLE001 — a missing note never costs the prompt
        return 0
    if text:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": text}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
