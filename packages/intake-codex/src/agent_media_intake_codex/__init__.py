"""Accept Codex notify JSON arguments, or legacy plain-text stdin.

`media-hook-codex event` is the hooks.json entry: one Claude-shaped event on
stdin (UserPromptSubmit, PreToolUse, Stop), handled as Claude Code's are —
the prompt becomes a "You:" turn, a tool call a step on the phone.
"""

import json
import re
import sys
from agent_media_core.types import Source
from agent_media_core.intake import run_hook_stdin as run


#: What a thread id has to look like to be usable as a session: a uuid, or
#: Hermes's clock stamp. Mirrors the server's `sessions._SESSION` — every
#: session-taking route rejects anything else with 400 "not a session id", so
#: a turn shelved under one is a thread nobody can open, archive or reply to.
_SESSION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                      r"|[0-9]{8}_[0-9]{6}_[0-9a-f]{4,}")


def _is_structured(text: str) -> bool:
    """A reply that is one JSON object is Codex talking to itself.

    Codex names a thread by running a hidden turn whose answer is
    `{"title": "…"}`, and notify reports it like any other — under its own
    thread id, so it was spoken aloud and shelved as a conversation of one
    line. Nobody's reply is a bare JSON object.
    """
    t = text.strip()
    if not (t.startswith("{") and t.endswith("}")):
        return False
    try:
        return isinstance(json.loads(t), dict)
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "event":
        from agent_media_core.intake.agent_events import main as events
        return events("codex")
    if not args:
        return run(Source.CODEX, "CODEX")
    try:
        payload = json.loads(args[0])
    except (ValueError, TypeError):
        print("media-hook-codex: invalid notification JSON", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print("media-hook-codex: notification must be an object", file=sys.stderr)
        return 1
    if payload.get("type") != "agent-turn-complete":
        return 0
    text = payload.get("last-assistant-message")
    if not isinstance(text, str) or not text.strip():
        return 0
    if _is_structured(text):
        return 0
    metadata = {}
    for key, target in (("thread-id", "session"), ("turn-id", "turn_id"),
                        ("cwd", "cwd")):
        value = payload.get(key)
        if not (isinstance(value, str) and value):
            continue
        if target == "session" and not _SESSION.fullmatch(value):
            # Codex itself always sends a uuid; a hand-made payload need not.
            # Speak the turn, but do not name a thread the app cannot address.
            print(f"media-hook-codex: ignoring thread-id {value!r} "
                  "(not a session id)", file=sys.stderr)
            continue
        metadata[target] = value
    return run(Source.CODEX, "CODEX", text=text, metadata=metadata)


if __name__ == "__main__":
    sys.exit(main())
