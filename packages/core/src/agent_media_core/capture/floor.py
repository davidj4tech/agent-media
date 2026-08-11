"""Publish floor transitions to the relay's D1 mirror.

Who holds the speech channel, and who has claimed David's next utterance, are
decided by local state on the acting host: marker files under `speech-hold.d/`
and the converse unix socket. That does not change here, and this module must
never be consulted to make either decision. Both answer in microseconds and
fail OPEN when absent — a missing marker means nobody holds it, so the worst
case is two voices overlapping rather than a channel silent forever. Putting
that behind a network call would force a choice between fail-open (authority
still local, nothing gained) and fail-closed (a Cloudflare blip silences the
house).

What the mirror buys is what the local files cannot:

  * History. Markers are reaped on expiry, so "have the per-owner holds been
    colliding now that there are three of us" had no answer at all on
    2026-08-11 — there was nothing anywhere to read.
  * Reach. Cece, or anything running without shell on this host, can see the
    floor instead of inferring it.

Every call here is fire-and-forget on a daemon thread: the publish costs a
round trip to Cloudflare and the hot path is the start of every spoken clip.
A publish that fails is logged and dropped. Off with MEDIA_FLOOR_MIRROR=0.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path


log = logging.getLogger(__name__)




def _script() -> list[str] | None:
    """relay-floor.sh, or None when the relay isn't installed on this host."""
    found = shutil.which("relay-floor")
    if found:
        return [found]
    fallback = Path.home() / "projects" / "tmux-relay" / "relay-floor.sh"
    return [str(fallback)] if fallback.is_file() else None


def _enabled() -> bool:
    return os.environ.get("MEDIA_FLOOR_MIRROR", "1") != "0"


def publish(channel: str, owner: str, action: str,
            ttl_s: float | None = None, note: str = "") -> None:
    """Record one transition. Returns immediately; never raises.

    `owner` is whatever the local mechanism already calls the holder — an
    explicit --owner, MEDIA_SPEECH_HOLD_OWNER, or the tmux pane. Inventing a
    second naming scheme here would make the mirror disagree with the thing it
    mirrors, which is worse than not having it.
    """
    if not _enabled():
        return
    cmd = _script()
    if cmd is None:
        return                      # no relay on this host; nothing to mirror
    argv = [*cmd, "publish", channel, owner or "unnamed", action,
            "" if ttl_s is None else f"{ttl_s:.0f}", note[:400]]
    # DETACHED, not a thread. The busiest publisher is `media speech-hold`,
    # which is a one-shot CLI: it sets the marker and exits in milliseconds,
    # while the publish is a round trip to Cloudflare. A daemon thread dies
    # with the interpreter and the row is simply never written — which is
    # exactly what happened the first time this was wired, and it looked like
    # the mirror silently ignoring the speech channel. A non-daemon thread
    # would work but would hold the CLI open for a second or two on every
    # hold, which is a real cost paid by an observability feature.
    #
    # The price is that the child's complaints go nowhere. Correct for a
    # mirror: there is no failure here worth delaying or interrupting anyone
    # over, and the local marker — the thing that actually gates speech — was
    # already written before we got here.
    try:
        subprocess.Popen(argv, start_new_session=True,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError as e:
        log.warning("floor mirror: %s", e)
