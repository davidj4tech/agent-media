"""Accept Codex notify JSON arguments, or legacy plain-text stdin."""

import json
import sys
from agent_media_core.types import Source
from agent_media_core.intake import run_hook_stdin as run


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
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
    metadata = {}
    for key, target in (("thread-id", "session"), ("turn-id", "turn_id"),
                        ("cwd", "cwd")):
        value = payload.get(key)
        if isinstance(value, str) and value:
            metadata[target] = value
    return run(Source.CODEX, "CODEX", text=text, metadata=metadata)


if __name__ == "__main__":
    sys.exit(main())
