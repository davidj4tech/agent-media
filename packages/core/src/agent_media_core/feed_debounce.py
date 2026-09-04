"""Put a turn on the shelf shortly after it is spoken, not when a timer looks.

This used to wait for a conversation to *finish*, because what it triggered
built one file out of the whole thing, and a feed cannot be updated per turn:
no podcast client re-fetches a guid it already has, so an episode published
mid-conversation is frozen at whatever version reached the client first. An
hour of silence was the only available proxy for "it is over".

What it triggers now appends: the turn's clips are linked into the library item
and the chapter list is rewritten (`media feed tracks`). Nothing is rebuilt and
nothing already delivered changes, so there is nothing left to wait for — the
delay exists only to let a burst of turns land together rather than scanning
the library once per sentence. `MEDIA_FEED_DEBOUNCE_S` sets it; a minute by
default.

Each turn arms a one-shot and cancels the one before it, so a run of quick
turns costs one scan. The poll stays installed as the safety net for turns that
never got to arm anything.

**Nothing here may delay a reply.** It runs on the speech path, so it is one
detached `sh -c` and no wait, wrapped in a bare except: the worst outcome of a
failure is the poll catching it later, and the worst outcome of *blocking* is
silence in the room.

Systemd only. The hosts that publish are the ones that hold speech history,
which today means the hub; a runit host falls back to the poll.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

log = logging.getLogger(__name__)

#: The transient unit, named so a second arming replaces the first rather than
#: stacking a queue of them.
UNIT = "agent-media-feed-debounce"

#: What it starts when it fires — the same oneshot the poll runs, so the
#: publish rules, the environment and the chain into Audiobookshelf live in
#: exactly one place.
TARGET_UNIT = "agent-media-feed-publish.service"

#: Default silence before a conversation counts as *finished*, in minutes.
#: Still the rule `feed publish-quiet` follows when a conversation is published
#: as one file by hand; no longer what this module waits for.
DEFAULT_QUIET_MIN = 60.0

#: How long to sit on a turn before appending it. Long enough that a burst of
#: turns is one library scan, short enough that a turn is listenable while the
#: conversation is still going — which is the whole point of an item that
#: appends.
DEFAULT_DEBOUNCE_S = 60.0


def quiet_s() -> float:
    try:
        minutes = float(os.environ.get("MEDIA_FEED_QUIET_MIN") or DEFAULT_QUIET_MIN)
    except ValueError:
        minutes = DEFAULT_QUIET_MIN
    return max(60.0, minutes * 60.0)


def debounce_s() -> float:
    try:
        seconds = float(os.environ.get("MEDIA_FEED_DEBOUNCE_S") or DEFAULT_DEBOUNCE_S)
    except ValueError:
        seconds = DEFAULT_DEBOUNCE_S
    # A floor, because the arming itself costs a systemd-run and the thing it
    # starts asks Audiobookshelf to scan; once a turn is plenty.
    return max(15.0, seconds)


def enabled() -> bool:
    """Only where a feed is served and systemd can hold the timer."""
    if not (os.environ.get("MEDIA_FEED_BASE_URL") or "").strip():
        return False
    return bool(shutil.which("systemd-run") and shutil.which("systemctl"))


def command(seconds: float) -> str:
    """The shell one-liner that re-arms the timer.

    `stop` first because `systemd-run --unit` refuses a name that already
    exists, and re-arming is the common case — every turn but the last one.
    """
    return (f"systemctl --user stop {UNIT}.timer 2>/dev/null; "
            f"exec systemd-run --user --collect --quiet --unit={UNIT} "
            f"--on-active={int(seconds)} "
            f"systemctl --user start --no-block {TARGET_UNIT}")


def arm(seconds: float | None = None) -> bool:
    """Push the publish deadline out to `seconds` from now. Never raises."""
    if not enabled():
        return False
    try:
        subprocess.Popen(["sh", "-c", command(seconds or debounce_s())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
        return True
    except Exception as e:  # noqa: BLE001 — see the module docstring
        log.debug("feed debounce not armed: %r", e)
        return False
