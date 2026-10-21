"""Speak each part of a question card as it comes up — the app's and the terminal's.

A card of several questions is read one part at a time: the PreToolUse hook
speaks the first, and each next one is spoken once the one before it is
answered (David, 19 Oct 2026: "can the next part not be read until the
previous one is answered"). Two places can answer a part:

- the app, which draws every part at once and sends them together, so it
  says when a part has been picked: POST /session/ask/part (`post`);
- the terminal, which shows one tab at a time: the hook starts this module
  as a detached watcher on the pane (`main`), which reads the screen and
  speaks a part when its tab becomes the current one.

Either way the part is spoken once (agent_media_core.pending_asks.claim_part)
by agent_media_core.intake.hook_claude_code.speak_ask_part.

    python -m agent_media_server.ask_follow <session> <pane> <tool_use_id>
"""

from __future__ import annotations

import os
import sys
import threading
import time

#: How often the watcher reads the pane, and how long the card may go unseen
#: before the watcher takes it as gone (a redraw, a resize, a scroll).
POLL_S = 0.7
GONE_S = 6.0
#: However it ends, the watcher ends by then.
MAX_S = float(os.environ.get("MEDIA_ASK_FOLLOW_MAX_S", str(6 * 3600)))


def _speak(session: str, part: int, tool_use_id: str = "", daemon: bool = True) -> str:
    """Speak it off the caller's thread: submitting waits on the playback
    queue, and neither a request nor the watcher should. The claim is taken
    here, first, so the answer says whether it will be spoken. The watcher
    passes `daemon=False`, so its exit waits for the speech it started."""
    from agent_media_core import pending_asks

    cur = pending_asks.read(session)
    if not cur:
        return "no_question"
    if tool_use_id and (cur.get("tool_use_id") or "") != tool_use_id:
        return "not_this_question"
    qs = cur.get("questions") or []
    if not 0 < part < len(qs):
        return "no_such_part"
    from agent_media_core.intake import hook_claude_code as hook

    done = threading.Event()
    why: list[str] = []

    def run() -> None:
        try:
            why.append(hook.speak_ask_part(session, part, tool_use_id))
        except Exception as e:  # noqa: BLE001 — a part unspoken is on the card
            why.append(f"failed: {e}")
        finally:
            done.set()

    threading.Thread(target=run, daemon=daemon).start()
    # The claim and the checks take a moment; the playback wait does not
    # count. A reason that comes back in time is the answer.
    done.wait(1.0)
    return why[0] if why else ""


def post(body: dict, bearer: str) -> tuple[bool, dict]:
    """POST /session/ask/part {"session"?, "part", "tool_use_id"?} (one of
    the two ids at least): the app
    has an answer picked for every part before `part`; speak `part`.
    `(ok, detail)`; ok with `spoken: false` and `why` when there was nothing
    to speak (already spoken, held for the screen, no such question)."""
    from . import auth, sessions

    session = str(body.get("session") or "").strip()
    tool_use_id = str(body.get("tool_use_id") or "")
    if not session and tool_use_id:
        # The app's card knows its question, not its thread.
        from agent_media_core import pending_asks

        session = pending_asks.session_of(tool_use_id)
        if not session:
            user, err = auth.gate(bearer)
            if not user:
                return False, err
            return True, {"spoken": False, "why": "no_question"}
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    try:
        part = int(body.get("part"))
    except (TypeError, ValueError):
        return False, {"error": "part must be a number", "status": 400}
    why = _speak(session, part, tool_use_id)
    return True, {"spoken": not why, **({"why": why} if why else {})}


def follow(session: str, pane: str, tool_use_id: str) -> None:
    """Watch `pane` until its card is answered, speaking each tab it reaches."""
    from agent_media_core import pending_asks

    from . import sessions

    started = time.monotonic()
    seen_at = started
    while time.monotonic() - started < MAX_S:
        cur = pending_asks.read(session)
        if not cur or (cur.get("tool_use_id") or "") != tool_use_id:
            return                           # answered, or another question
        approval = sessions.approval_for(pane, cur.get("harness") or "claude", session)
        now = time.monotonic()
        if approval and approval.get("kind") == "question" and approval.get("tool_use_id") == tool_use_id:
            seen_at = now
            at = approval.get("current")
            if not approval.get("review") and isinstance(at, int) and at > 0:
                _speak(session, at, tool_use_id, daemon=False)
        elif now - seen_at > GONE_S:
            return
        time.sleep(POLL_S)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: python -m agent_media_server.ask_follow <session> <pane> <tool_use_id>",
              file=sys.stderr)
        return 2
    follow(*argv)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
