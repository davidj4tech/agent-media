"""The Driver seam: who holds a session's agent process, and how we reach it.

docs/proposals/2026-09-22-headless-sessions.md §2. Two kinds:

* **pane** (`pane.py`) — today's path, wrapped unchanged: the agent is a TUI
  in a tmux or herdr pane, typed into with `send-keys` and read off the
  screen. Desk sessions, and everything when headless is switched off.
* **headless** (`headless.py`) — the agent is a `claude -p` stream-json
  process owned by `media-sessiond` (sessiond.py), reached over its unix
  socket. Messages are JSON lines, permission requests are structured, and
  state comes from the process's own events rather than a screen.

The routes ask `for_session(session)` which driver owns a thread, and
`for_new(agent)` which one starts a fresh chat. With `MEDIA_HEADLESS` unset
(the default) both always answer the pane driver and nothing here touches
sessiond — the flag off is no behaviour change at all.

Every method answers `(ok, detail)` the way the routes do: `detail` carries
`status` on failure. Gating (`auth.gate`) is the route's, done before a driver
is asked anything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

PANE = "pane"
HEADLESS = "headless"


@dataclass(frozen=True)
class Caps:
    """What a driver can do for its harness — so a route says "not supported"
    from a flag rather than a special case."""
    interrupt: bool = True
    structured_approvals: bool = False
    multi_select: bool = False
    free_text_answers: bool = False
    live_events: bool = False


class Driver(Protocol):
    kind: str
    caps: Caps

    def start(self, *, agent: str, cwd: str, text: str, host: str = "",
              flags: list[str] | tuple[str, ...] = (), quote: str = "",
              model: str = "", mode: str = "") -> tuple[bool, dict]:
        """A fresh session with `text` as its first message, on `model` (an
        alias, "" for the default) and in plan mode when `mode` is "plan"."""

    def configure(self, session: str, *, model: str | None = None,
                  mode: str | None = None) -> tuple[bool, dict]:
        """Change a live or parked session's model or plan mode; None leaves
        that one alone."""

    def send(self, session: str, body: str, text: str, *, quote: str = "") -> tuple[bool, dict]:
        """`body` into the session (reviving it if it has ended). `text` is the
        listener's own words, shelved as their turn; `quote`, when the driver
        can carry it separately, rides as its own paragraph."""

    def resume(self, session: str) -> tuple[bool, dict]:
        """Bring an ended session back, saying nothing."""

    def interrupt(self, session: str) -> tuple[bool, dict]:
        """Stop the running turn. `{"interrupted": bool, "why", "state"}`."""

    def answer(self, session: str, request: dict) -> tuple[bool, dict]:
        """Answer what the session is waiting on. `request` is the route's
        body: `{"choice", "key"}` (numbered) or `{"request_id", "decision",
        "answers"?, "message"?}` (structured)."""

    def close(self, session: str) -> tuple[bool, dict]:
        """End the session; the transcript stays."""

    def state(self, session: str) -> dict:
        """`{"state": working|waiting|approval|ended, "live": bool, "pane"}`."""

    def approval(self, session: str) -> dict | None:
        """What the session is stopped on, or None."""


def headless_enabled() -> bool:
    """`MEDIA_HEADLESS=1`: chats started from the app run headless. Off by
    default; read on every call so a test (or a restart with a new env) needs
    nothing else."""
    return (os.environ.get("MEDIA_HEADLESS") or "").strip().lower() in ("1", "true", "yes", "on")


_PANE = None
_HEADLESS = None


def pane_driver():
    global _PANE
    if _PANE is None:
        from .pane import PaneDriver

        _PANE = PaneDriver()
    return _PANE


def headless_driver():
    global _HEADLESS
    if _HEADLESS is None:
        from .headless import HeadlessDriver

        _HEADLESS = HeadlessDriver()
    return _HEADLESS


def owned_headless(session: str) -> bool:
    """Whether sessiond owns `session` (live, parked or closed). Always False
    with the flag off. Read from sessiond's records on disk, so it answers the
    same whether or not sessiond is up — a headless thread must never be
    revived in a pane just because its host is down."""
    if not headless_enabled() or not session:
        return False
    return headless_driver().owns(session)


def for_session(session: str) -> Driver:
    """The driver that owns `session`: headless when sessiond has a record of
    it (and the flag is on), else the pane driver."""
    return headless_driver() if owned_headless(session) else pane_driver()


def for_new(agent: str = "claude") -> Driver:
    """The driver a fresh chat from the app starts in: headless when the flag
    is on and the agent has a headless adapter (Claude, for now), else pane."""
    if headless_enabled() and agent in headless_driver().agents:
        return headless_driver()
    return pane_driver()


def headless_state(session: str) -> dict | None:
    """sessiond's view of `session` — `{"state", "live", "pane": None,
    "approval", "pid"}` — or None when it is not a headless session (or the
    flag is off). For the readers (`/conversation`, the log, the stream)."""
    if not owned_headless(session):
        return None
    return headless_driver().view(session)


def _reset_for_tests() -> None:
    global _PANE, _HEADLESS
    _PANE = _HEADLESS = None
