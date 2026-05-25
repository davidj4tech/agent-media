"""pi coding-agent intake — stdin-pipe hook."""

import sys
from ..types import Source
from ._hook_stdin import run


def main() -> int:
    return run(Source.PI, "PI")


if __name__ == "__main__":
    sys.exit(main())
