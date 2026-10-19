"""A person's own notification setting, asked where a notice is decided
(David, 20 Oct 2026: "a little more control over specific contact's
notifications").

The setting lives with the contacts, on the server (agent_media_server.
contacts_notify), per channel: `chats` (their Matrix rooms: SMS, Messenger,
WhatsApp …) and `email`. Each is "default" (the room's own speech level
decides, as before), "priority" (always a notice, even in a room left
quiet) or "quiet" (kept in the lists, never a notice nor read out).

The intake asks `mode(thread, "chats")` and turns its decision with
`adjust`. The server registers the resolver when it starts its contacts
(`set_resolver`); on a host with no server (the standalone Matrix intake)
nothing is registered and every person is "default".
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

log = logging.getLogger(__name__)

MODES = ("default", "priority", "quiet")
CHANNELS = ("chats", "email")

#: (key, channel) → mode. `key` is a thread id (a Matrix room's) for
#: "chats", an address for "email".
Resolver = Callable[[str, str], Optional[str]]
_RESOLVER: Optional[Resolver] = None


def set_resolver(fn: Optional[Resolver]) -> None:
    global _RESOLVER
    _RESOLVER = fn


def mode(key: str, channel: str) -> str:
    """The person's mode on `channel` for `key`; "default" when there is no
    person, no setting, no resolver or the resolver fails."""
    fn = _RESOLVER
    if fn is None or not key:
        return "default"
    try:
        got = fn(key, channel)
    except Exception as e:  # noqa: BLE001 — a notice decided as before, not none
        log.warning("contact notify: lookup failed: %s", e)
        return "default"
    return got if got in MODES else "default"


def adjust(decision: str, how: str) -> str:
    """An intake decision ("play", "toast" or "hold") turned by a person's
    mode: "quiet" holds it with no notice; "priority" makes a held one a
    notice (a toast) and leaves one that plays alone."""
    if how == "quiet":
        return "hold"
    if how == "priority" and decision == "hold":
        return "toast"
    return decision
