"""Report failures to agent-media, when it happens to be installed.

This bridge is the only path between speaking and a tmux pane, and when it
breaks it breaks quietly: HA gets its 200, the phone says something reassuring,
and the words go nowhere. That is exactly how injection into a tmux session
that no longer existed stayed broken for weeks — the failure was in the journal
the whole time, and nobody tails the journal.

So a failure now lands in agent-media's error table (`media errors`, and the
MCP `errors` tool) and raises a throttled notification.

agent-media is optional. This package installs and runs standalone — voice
injection into tmux is useful with no agent-media anywhere — so every function
here degrades to a no-op when core isn't importable, and never raises into the
request path. A missing observability backend must not cost you your words.
"""

from __future__ import annotations

import sys

COMPONENT = "voice-bridge"


def _core():
    """agent_media_core, or None when it isn't installed."""
    try:
        import agent_media_core  # noqa: F401
    except ImportError:
        return None
    return agent_media_core


def report(message: str, *, notify: bool = True, **extras) -> None:
    """Record a failure. Best-effort: never raises, never blocks delivery.

    `notify` sends a throttled desktop/phone notification as well — reserve it
    for things that mean "your voice input is not working", not for expected
    misses.
    """
    print(f"[shim] {message}" + (f" {extras}" if extras else ""),
          file=sys.stderr, flush=True)
    if _core() is None:
        return
    try:
        from agent_media_core.state import StateStore
        StateStore().log_error(COMPONENT, message, extras=extras or None)
    except Exception:  # noqa: BLE001 - observability must never break injection
        pass
    if not notify:
        return
    try:
        from agent_media_core._notify import notify as _notify
        _notify(key="voice-bridge-failure",
                title="voice-bridge: transcript not delivered",
                content=message[:200],
                throttle=300)
    except Exception:  # noqa: BLE001
        pass
