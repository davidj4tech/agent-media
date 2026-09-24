"""Codex's and pi's session events, handled the way Claude Code's are.

Claude Code tells agent-media three things besides the reply it speaks: what
the person typed (UserPromptSubmit → a "You:" turn in the transcript), what
the session is doing (PreToolUse → the step list on the phone), and when it
is done (Stop). Codex's hooks send the same events with the same fields; the
pi extension (packages/core/pi/media-tts.ts) sends them in that shape too. So
one handler serves both, and the transcript reads the same whichever agent
held the conversation.

pi adds SessionStart: it is the only record of which pi is on which session
(see harnesses.register_pane).
"""

from __future__ import annotations

import json
import logging
import os
import sys

log = logging.getLogger(__name__)


def handle(payload: dict, harness: str) -> int:
    """One event. Never raises; a hook must not break the agent."""
    if not isinstance(payload, dict):
        return 0
    event = str(payload.get("hook_event_name") or "")
    session = str(payload.get("session_id") or "")
    try:
        if harness == "codex":
            from .. import harnesses

            # A script driving Codex (`codex exec`, a run from /tmp) is not a
            # conversation: nothing of it is recorded, spoken or shelved.
            if harnesses.codex_run_scripted(session, str(payload.get("cwd") or "")):
                return 0
        if event == "SessionStart":
            from .. import harnesses

            pane = str(payload.get("pane") or os.environ.get("TMUX_PANE") or "")
            pid = int(payload.get("pid") or os.getppid())
            harnesses.register_pane(harness, session, pid, pane)
            return 0
        if event in ("UserPromptSubmit", "PreToolUse", "Stop"):
            from .. import activity

            activity.record(payload)
        if event == "UserPromptSubmit":
            from .hook_claude_code import _handle_user_prompt

            return _handle_user_prompt(payload)
    except Exception as e:  # noqa: BLE001
        log.warning("%s hook: %s failed: %s", harness, event, e)
    return 0


def main(harness: str) -> int:
    """`media-hook-<harness> event`: one JSON event on stdin."""
    if os.environ.get("MEDIA_HOOK_ENABLED", "1") == "0":
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    return handle(payload, harness)
