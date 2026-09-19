"""pi coding-agent intake — stdin-pipe hook.

    media-hook-pi [--session <id>]   the reply on stdin, spoken; the session
                                     id files it under its conversation
    media-hook-pi event              one Claude-shaped event on stdin (see
                                     agent_events), from the pi extension
"""

import sys
from ..types import Source
from ._hook_stdin import run


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "event":
        from .agent_events import main as events
        return events("pi")
    metadata = {}
    if len(args) >= 2 and args[0] == "--session" and args[1]:
        metadata["session"] = args[1]
    return run(Source.PI, "PI", metadata=metadata or None)


if __name__ == "__main__":
    sys.exit(main())
