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

Gemini CLI's hooks have names of their own (BeforeAgent, BeforeTool,
AfterAgent); intake/hook_gemini.py renames them and hands them here too.
Grok's are Claude Code's names with camelCase fields beside the snake_case
ones; intake/hook_grok.py hands them here.
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
        if (harness == "codex" and event in ("PreToolUse", "PostToolUse", "PostToolUseFailure")
                and str(payload.get("tool_name") or "").split(".")[-1] == "request_user_input"):
            from .hook_claude_code import _handle_pretooluse, _handle_posttooluse

            # Share question storage, speech, holding and part-by-part
            # read-out. Only the terminal's answer keys differ.
            question = {**payload, "tool_name": "AskUserQuestion", "harness": "codex"}
            if event == "PreToolUse":
                return _handle_pretooluse(question)
            response = payload.get("tool_response") or {}
            if isinstance(response, str):
                try:
                    response = json.loads(response)
                except ValueError:
                    response = {}
            if isinstance(response, dict):
                answers = response.get("answers") or {}
                question["tool_response"] = {"answers": {
                    q["question"]: ", ".join((answers.get(q.get("id")) or {}).get("answers") or [])
                    for q in (payload.get("tool_input") or {}).get("questions") or []}}
            return _handle_posttooluse(question)
        if event == "UserPromptSubmit":
            from .hook_claude_code import _handle_user_prompt

            if harness == "codex":
                # How full the context is: printed here, by the hook Codex
                # already trusts (intake/context.py).
                from .context import note_for

                note = note_for(payload)
                if note:
                    print(note)
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
