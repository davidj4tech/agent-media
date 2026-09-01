"""Publish a conversation when it stops, not when a timer next looks.

A feed cannot be updated per turn: no podcast client re-fetches a guid it
already has, so an episode published mid-conversation is frozen at whatever
version reached the client first. What *can* be immediate is the moment after
the last turn — and the machine knows when that is, because every turn writes a
history row.

So each turn arms a one-shot for `MEDIA_FEED_QUIET_MIN` minutes' time and
cancels the one before it. Keep talking and the deadline keeps moving; stop,
and the episode is built as soon as the silence is long enough to mean
something. The five-minute poll stays installed as the safety net for turns
that never got to arm anything.

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

#: Default silence before a conversation counts as finished, in minutes. The
#: publisher reads the same variable, so the debounce and the thing it triggers
#: can never disagree about what "finished" means.
DEFAULT_QUIET_MIN = 60.0


def quiet_s() -> float:
    try:
        minutes = float(os.environ.get("MEDIA_FEED_QUIET_MIN") or DEFAULT_QUIET_MIN)
    except ValueError:
        minutes = DEFAULT_QUIET_MIN
    return max(60.0, minutes * 60.0)


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
        subprocess.Popen(["sh", "-c", command(seconds or quiet_s())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
        return True
    except Exception as e:  # noqa: BLE001 — see the module docstring
        log.debug("feed debounce not armed: %r", e)
        return False
