"""Codex (OpenAI CLI) intake — stdin-pipe hook."""

import sys
from agent_media_core.types import Source
from agent_media_core.intake import run_hook_stdin as run


def main() -> int:
    return run(Source.CODEX, "CODEX")


if __name__ == "__main__":
    sys.exit(main())
