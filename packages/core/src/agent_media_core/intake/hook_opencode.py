"""opencode intake — called by the agent-media plugin (packages/core/opencode).

    media-hook-opencode --session <id>   the turn is over: its reply, read from
                                         opencode's database, is spoken and
                                         filed under its conversation
    media-hook-opencode event            one Claude-shaped event on stdin (see
                                         agent_events), from the plugin

opencode tells a plugin a session went idle, not what it said, so the reply
is not piped in as pi's is: it is read back from the database, which has it
by then (`harnesses.opencode_last_reply`). The last step's id is kept per
session, so an idle heard twice for one turn is spoken once.
"""

from __future__ import annotations

import sys

from ..types import Source
from ._hook_stdin import run


def _seen_path(session: str):
    from .._paths import state_dir

    return state_dir() / "opencode-spoken" / session


def _already_spoken(session: str, message: str) -> bool:
    """Whether `message` was the last one spoken for `session`; marks it."""
    path = _seen_path(session)
    try:
        if path.read_text().strip() == message:
            return True
    except OSError:
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(message)
    except OSError:
        pass
    return False


def _headless_workspace(session: str) -> None:
    """A headless opencode chat runs in the one `opencode serve` sessiond
    holds for all of them (agent_media_server/sessiond_opencode.py), so the
    server's environment cannot say whose workspace a reply belongs to, as a
    `claude -p`'s does. sessiond's record of the session can: its workspace is
    put where the submitter looks (`MEDIA_SOURCE_WORKSPACE`), for the
    conversation's own voice and mute. Only under the server's
    `MEDIA_SOURCE_KIND=headless`."""
    import json
    import os

    from .._paths import state_dir

    if os.environ.get("MEDIA_SOURCE_KIND") != "headless" or os.environ.get("MEDIA_SOURCE_WORKSPACE"):
        return
    try:
        rec = json.loads((state_dir() / "sessiond" / f"{session}.json").read_text())
    except (OSError, ValueError):
        return
    workspace = str(rec.get("workspace") or "").strip() if isinstance(rec, dict) else ""
    if workspace:
        os.environ["MEDIA_SOURCE_WORKSPACE"] = workspace


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "event":
        from .agent_events import main as events
        return events("opencode")
    if len(args) < 2 or args[0] != "--session":
        return 0
    from .. import harnesses

    session = args[1]
    if not harnesses.is_opencode(session):
        return 0
    # A subagent's session goes idle as well; its words are its parent's
    # tool result, not something said to the person.
    if harnesses.opencode_rows(
            "select 1 from session where id = ? and parent_id is not null", (session,)):
        return 0
    message, text = harnesses.opencode_last_reply(session)
    if not text or _already_spoken(session, message):
        return 0
    _headless_workspace(session)
    return run(Source.OPENCODE, "OPENCODE", text=text, metadata={"session": session})


if __name__ == "__main__":
    sys.exit(main())
