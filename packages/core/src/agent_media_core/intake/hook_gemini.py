"""Gemini CLI intake — all four of its hooks, one command.

    media-hook-gemini      one Gemini hook event on stdin (`hook_event_name`)

Gemini's hooks say what Claude Code's do, under other names, so each is
renamed and handed to agent_events — the transcript, the step list and the
voice then work the same whichever agent held the conversation:

    SessionStart → SessionStart       which pane this Gemini is in
                                      (harnesses.register_pane)
    BeforeAgent  → UserPromptSubmit   the person's words, a turn begins; and
                                      how full the context is
                                      (intake/context.note_for), printed as
                                      the JSON Gemini reads into the turn
    BeforeTool   → PreToolUse         a step on the phone's list
    AfterAgent   → Stop               the turn is over: its reply
                                      (`prompt_response`) is spoken and filed
                                      under its conversation

The context note used to be a BeforeAgent hook of its own
(`media-hook-context`); it is folded in here so there is one BeforeAgent
entry, and `media-setup profile` replaces the old one (setup._row_gemini).

Gemini runs a hook through a shell and waits on it — for its stdout to
close, and for an AfterAgent's answer, since that one can send the model
back for another try. What the hook prints is read as its answer: stdout,
or stderr when stdout is empty, and plain text there is shown to the person.
So nothing is printed but the context note's JSON, stderr goes nowhere, and
the reply is spoken from a detached child: speaking takes as long as the
reply does, and Gemini would hold its prompt for all of it.

A `gemini -p` run is a script asking a question, not a conversation (as
`codex exec` is): it still gets the context note, but nothing of it is
recorded, spoken or shelved.
"""

from __future__ import annotations

import json
import logging
import os
import sys

from ..types import Source

log = logging.getLogger(__name__)

#: What Gemini hands AfterAgent when the model said nothing in words.
_NO_TEXT = "[no response text]"

#: Gemini's event names, as agent_events knows them.
_AS = {"SessionStart": "SessionStart", "BeforeAgent": "UserPromptSubmit",
       "BeforeTool": "PreToolUse", "AfterAgent": "Stop"}


def _parent(pid: int) -> int:
    """`pid`'s parent, from /proc; 0 when it cannot be read."""
    try:
        stat = open(f"/proc/{pid}/stat", "rb").read().decode(errors="replace")
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def gemini_process(start: int | None = None, depth: int = 6) -> tuple[int, list[str]]:
    """`(pid, argv)` of the Gemini this hook runs under, or `(0, [])`.

    Gemini starts a hook through a shell (`sh -c "<command>"`), so the hook's
    parent is the shell, or Gemini itself when the shell exec'd the command
    — walked up a few levels until a process is Gemini. The first one found
    is the relaunched child, the one holding the conversation.
    """
    from .. import harnesses

    pid = os.getppid() if start is None else start
    for _ in range(depth):
        if pid <= 1:
            break
        argv = harnesses._argv(str(pid))
        if harnesses._gemini_pid(argv):
            return pid, argv
        pid = _parent(pid)
    return 0, []


def _scripted(argv: list[str], cwd: str) -> bool:
    """A `gemini -p …` one-shot, or a run in a temp directory: a script."""
    from .. import harnesses

    if harnesses._in_tmp(cwd):
        return True
    return any(a in ("-p", "--prompt") or a.startswith("--prompt=") for a in argv[1:])


def _detached(fn) -> None:
    """Run `fn` in a child of its own, so the hook can answer at once.
    `MEDIA_HOOK_NO_DETACH` runs it here instead (the tests)."""
    if os.environ.get("MEDIA_HOOK_NO_DETACH"):
        fn()
        return
    try:
        pid = os.fork()
    except OSError:
        fn()
        return
    if pid > 0:
        return
    try:
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
    except OSError:
        pass
    try:
        fn()
    finally:
        os._exit(0)


def _speak(text: str, session: str) -> None:
    from ._hook_stdin import run

    run(Source.GEMINI, "GEMINI", text=text,
        metadata={"session": session} if session else None)


def handle(payload: dict, *, out=None) -> int:
    """One Gemini event. Never raises: a hook must not break the agent."""
    from .agent_events import handle as handle_event

    if not isinstance(payload, dict):
        return 0
    out = out or sys.stdout
    event = str(payload.get("hook_event_name") or "")
    if event not in _AS:
        return 0
    try:
        if event == "BeforeAgent":
            # First, and flushed: the turn's recording below forks.
            from .context import note_for

            note = note_for(payload)
            if note:
                out.write(note + "\n")
                out.flush()
        pid, argv = gemini_process()
        if _scripted(argv, str(payload.get("cwd") or "")):
            return 0
        as_claude = {**payload, "hook_event_name": _AS[event]}
        if event == "SessionStart" and pid:
            as_claude["pid"] = pid
        handle_event(as_claude, "gemini")
        if event == "AfterAgent":
            text = str(payload.get("prompt_response") or "").strip()
            session = str(payload.get("session_id") or "")
            if text and text != _NO_TEXT:
                _detached(lambda: _speak(text, session))
    except Exception as e:  # noqa: BLE001
        log.warning("gemini hook: %s failed: %s", event, e)
    return 0


def main(argv: list[str] | None = None) -> int:
    # Gemini reads stderr as the hook's answer when stdout is empty, and
    # shows plain text to the person: no log line may reach it.
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
