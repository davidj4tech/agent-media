"""Claude Code's own record of its running sessions.

Every interactive `claude` writes `~/.claude/sessions/<pid>.json` — its
session id, cwd and tmux pane (`"<session>:@<window>.%<pane>"`) — and keeps
it current. The pane registry (`~/.claude/tmux-sessions/<pane>`) is ours,
written by a SessionStart hook and deleted by SessionEnd, and it loses
entries: a live runlet session went missing from it on 2026-09-19 and so was
never "live" to the app. This file is Claude's, so it is asked first.

CLAUDE_SESSIONS_DIR overrides the location (tests).
"""

from __future__ import annotations

import glob
import json
import os
from typing import Optional


def _root() -> str:
    return os.path.expanduser(os.environ.get("CLAUDE_SESSIONS_DIR") or "~/.claude/sessions")


def _is_claude(pid: int) -> bool:
    """Still a claude: a pid file outlives a crash, and pids are reused."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            argv0 = fh.read().split(b"\0", 1)[0]
    except OSError:
        return False
    return os.path.basename(argv0.decode(errors="replace")) == "claude"


def session_for_pid(pid: int) -> Optional[str]:
    """The session id the claude with this pid is running, or None."""
    try:
        with open(os.path.join(_root(), f"{pid}.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    sid = data.get("sessionId") if isinstance(data, dict) else None
    return str(sid) if sid else None


def by_pane() -> dict[str, str]:
    """`{pane id ("%518"): session id}` for every live claude in a tmux pane."""
    out: dict[str, str] = {}
    for path in glob.glob(os.path.join(_root(), "*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            pid = int(data.get("pid") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        tmux = str(data.get("tmux") or "")
        sid = str(data.get("sessionId") or "")
        pane = tmux.rsplit(".", 1)[-1] if "." in tmux else ""
        if sid and pane.startswith("%") and _is_claude(pid):
            out[pane] = sid
    return out
