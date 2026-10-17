"""Grok intake — its hooks, one command.

    media-hook-grok      one Grok hook event on stdin (`hook_event_name`)

Grok's hooks are Claude Code's in all but a few names and fields: it sends
camelCase keys with Claude's snake_case spelling beside them (`session_id`,
`tool_name`, `tool_input`, `transcript_path`), and `hook_event_name` is the
PascalCase name. So each event is handed to agent_events under Claude Code's
name, and the transcript, the step list and the voice work the same
whichever agent held the conversation:

    SessionStart      which pane this Grok is in (harnesses.register_pane)
    UserPromptSubmit  the person's words, a turn begins
    PreToolUse        a step on the phone's list
    PostToolUse       how full the context is (intake/context.note_for),
                      printed as the JSON Grok hands the model with the
                      tool's result — the only events whose
                      `additionalContext` reaches the model are the tool
                      ones, so the note cannot go on the prompt as it does
                      for the others
    Stop              the turn is over: its reply (`lastAssistantMessage`)
                      is spoken and filed under its conversation. Only
                      `reason == "end_turn"` is a reply: Grok fires another
                      Stop at session end (`channel_closed`, `shutdown`)

An interrupted or failed turn (StopCancelled, StopFailure) is not taken, as
Claude Code's is not: its report can arrive after the next prompt has
begun, and would end that turn on the phone instead.

Grok runs every hook in the foreground — it reads no `async` — and Stop is
a gate on the turn's own path, so the hook does nothing slow before it
answers: the reply is spoken from a detached child (hook_gemini._detached),
in a session of its own, so Grok's teardown killing its hooks does not take
it too. Grok reads a PostToolUse hook's stdout as its reply and an exit 2's
stderr as feedback for the model, so nothing is printed but the context
note's JSON, stderr goes nowhere, and it always exits 0.

A subagent's events carry `subagentType`: its turns are its parent's
business, not a conversation of their own, and are left alone. So is a
`grok -p` run, a script asking a question (as `codex exec` is).

Read from Grok 1.0.62's hooks guide (docs/user-guide/10-hooks.md); no event
has been seen from a real Grok yet. Where it does not name a field (the
prompt on UserPromptSubmit), the Claude name is read and the camelCase one
after it.
"""

from __future__ import annotations

import json
import logging
import os
import sys

from ..types import Source
from .hook_gemini import _detached, _parent

log = logging.getLogger(__name__)

#: The events this hook takes, as agent_events knows them. PostToolUse is
#: the context note's alone.
_AS = {"SessionStart": "SessionStart", "UserPromptSubmit": "UserPromptSubmit",
       "PreToolUse": "PreToolUse", "Stop": "Stop", "PostToolUse": ""}

#: grok's own snake_case event names (`hookEventName`), for a payload that
#: has only those.
_SNAKE = {"session_start": "SessionStart", "user_prompt_submit": "UserPromptSubmit",
          "pre_tool_use": "PreToolUse", "post_tool_use": "PostToolUse", "stop": "Stop"}


def grok_process(start: int | None = None, depth: int = 6) -> tuple[int, list[str]]:
    """`(pid, argv)` of the Grok this hook runs under, or `(0, [])`. Grok
    runs a command hook as `sh -c`, so the hook's parent is the shell, or
    Grok itself when the shell exec'd it — walked up until one is Grok."""
    from .. import harnesses

    pid = os.getppid() if start is None else start
    for _ in range(depth):
        if pid <= 1:
            break
        argv = harnesses._argv(str(pid))
        if harnesses._grok_pid(argv):
            return pid, argv
        pid = _parent(pid)
    return 0, []


def _scripted(argv: list[str], cwd: str) -> bool:
    """A `grok -p …` one-shot (or its prompt from a file or JSON), or a run
    in a temp directory: a script."""
    from .. import harnesses

    if harnesses._in_tmp(cwd):
        return True
    return any(a in ("-p", "--single", "--prompt-file", "--prompt-json")
               or a.startswith(("--single=", "--prompt-file=", "--prompt-json="))
               for a in argv[1:])


def _speak(text: str, session: str) -> None:
    from ._hook_stdin import run

    run(Source.GROK, "GROK", text=text,
        metadata={"session": session} if session else None)


def _context_note(payload: dict) -> str:
    """The context note for this session now, as Grok's PostToolUse reply,
    or "". The numbers are read from Grok's own files (`grok_usage`)."""
    from .context import grok_usage, note_for

    transcript = str(payload.get("transcript_path") or payload.get("transcriptPath") or "")
    got = grok_usage(transcript) if transcript and os.path.exists(transcript) else None
    if not got:
        return ""
    used, window = got
    return note_for({**payload, "hook_event_name": "PostToolUse",
                     "context_tokens": used, "context_window": window})


def handle(payload: dict, *, out=None) -> int:
    """One Grok event. Never raises: a hook must not break the agent."""
    from .agent_events import handle as handle_event

    if not isinstance(payload, dict):
        return 0
    out = out or sys.stdout
    event = str(payload.get("hook_event_name") or "")
    if event not in _AS:
        event = _SNAKE.get(str(payload.get("hookEventName") or ""), event)
    if event not in _AS or payload.get("subagentType"):
        return 0
    session = str(payload.get("session_id") or payload.get("sessionId")
                  or os.environ.get("GROK_SESSION_ID") or "")
    try:
        pid, argv = grok_process()
        if _scripted(argv, str(payload.get("cwd") or "")):
            return 0
        if event == "PostToolUse":
            note = _context_note({**payload, "session_id": session})
            if note:
                out.write(note + "\n")
                out.flush()
            return 0
        if event == "Stop" and payload.get("reason") not in (None, "end_turn"):
            return 0        # the session-end fire: no turn ended
        as_claude = {**payload, "hook_event_name": _AS[event], "session_id": session,
                     "tool_name": payload.get("tool_name") or payload.get("toolName"),
                     "tool_input": payload.get("tool_input") or payload.get("toolInput"),
                     "prompt": payload.get("prompt") or payload.get("userPrompt") or ""}
        if event == "SessionStart" and pid:
            as_claude["pid"] = pid
        if event == "Stop":
            # The turn's end is recorded here (a line in a file); the reply
            # is spoken off Grok's clock.
            handle_event(as_claude, "grok")
            text = str(payload.get("lastAssistantMessage")
                       or payload.get("last_assistant_message") or "").strip()
            if text:
                _detached(lambda: _speak(text, session))
            return 0
        handle_event(as_claude, "grok")
    except Exception as e:  # noqa: BLE001
        log.warning("grok hook: %s failed: %s", event, e)
    return 0


def main(argv: list[str] | None = None) -> int:
    # Grok feeds a PostToolUse or Stop hook's stderr to the model on exit 2,
    # and shows a failing hook's first stderr line: no log line may reach it.
    try:
        sys.stderr = open(os.devnull, "w")
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2)
    except OSError:
        pass
    if os.environ.get("MEDIA_HOOK_ENABLED", "1") == "0":
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    return handle(payload)


if __name__ == "__main__":
    sys.exit(main())
