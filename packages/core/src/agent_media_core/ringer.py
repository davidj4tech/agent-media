"""Publish whether the phone is meant to be making noise.

## The fact, and who can see it

An alert spoken at 08:45 into a phone deliberately set to silent is the stack
working exactly as designed and being wrong anyway. The gate that fixes it
lives in ``intake.submit`` on the *origin* host; the state it needs lives on
the phone, behind an API only an Android app can call. This service is the
bridge — the same shape as :mod:`agent_media_core.mic_block`, and for the same
reason: it is the only process in the fleet positioned to read the thing.

## Two questions, because one of them is not enough

``AudioManager.getRingerMode()`` answers the *ringer switch*: silent, vibrate,
normal. It is not the whole answer. On modern Android, Do Not Disturb does not
move the ringer mode — a phone in DND reports ``normal`` while being, in every
sense the person holding it means, on silent. The API for that is
``NotificationManager.getCurrentInterruptionFilter()``, which needs
``ACCESS_NOTIFICATION_POLICY``: a user grant through a settings intent, and
notably *not* the notification-listener access that Play Protect refuses to
sideloaded apps.

So the companion's ``/ringer`` answers both, and this decides from both:

    quiet  ⇔  mode is silent-or-vibrate
              OR the interruption filter is anything but `all`, while granted

An ungranted filter reports ``dnd=unknown`` and contributes nothing. A question
nobody has answered must never be the reason a phone goes quiet.

## Why the verdict travels on the speech broker

The origin needs this per alert, and ``/ringer`` is bound to 127.0.0.1 like the
rest of the companion's status port — deliberately, so it cannot be reached
from red5 at all. Rather than open a port or put ssh in the say path, the
verdict is written into the phone's mpv broker as ``user-data``: the channel
this codebase already uses for a fact every host must agree on (see
``sinks.speech._BROKER_OWNER_KEY``, "stored in mpv `user-data` on the broker
itself, so all hosts see the same value"). The origin then reads it off a
socket it was about to talk to anyway.

The payload carries its own ``checked_at`` so a stale publisher cannot silence
anything: the reader ages it out and speaks. Everything here fails towards
sound.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.request
from pathlib import Path

from ._paths import state_dir

log = logging.getLogger(__name__)

#: Where the companion answers. Same port as ``/mic``, same loopback bind.
DEFAULT_URL = "http://127.0.0.1:8770/ringer"

#: How often to ask.
#:
#: Not a race with anything. The cost of being late is one alert spoken into a
#: phone silenced in the last few seconds, or one withheld from a phone just
#: taken off silent — and the second is the reason not to make this minutes:
#: someone who turns the ringer back on expects the next thing to be audible.
DEFAULT_INTERVAL_S = 20.0

#: Ringer modes that mean "do not make noise at this person".
QUIET_MODES = ("silent", "vibrate")

#: The interruption filter that means "everything gets through". Any other
#: value — priority, none, alarms — is a person asking not to be disturbed.
FILTER_ALL = "all"


def url() -> str:
    return os.environ.get("MEDIA_RINGER_URL", DEFAULT_URL).strip()


def interval_s() -> float:
    try:
        return max(2.0, float(os.environ.get("MEDIA_RINGER_INTERVAL_S",
                                             DEFAULT_INTERVAL_S)))
    except ValueError:
        return DEFAULT_INTERVAL_S


def parse(body: str) -> dict | None:
    """One line from ``/ringer`` into a reading, or None if it is not one.

    The wire format is a mode followed by loose ``key=value`` fields::

        silent dnd=priority granted=1
        normal dnd=unknown granted=0

    Loose because the same line is read over ssh by a person, and because a
    field this side does not know about must be free to appear without a
    version negotiation. Unknown keys are ignored; a missing key is absent, not
    false.

    None means "that was not an answer", which is never the same as "not
    quiet": an app that replied with an error page must leave whatever the
    reader last believed alone rather than assert the phone is audible.
    """
    fields = (body or "").strip().split()
    if not fields:
        return None
    mode = fields[0].strip().lower()
    if not mode or "=" in mode:
        return None
    out: dict = {"mode": mode}
    for field in fields[1:]:
        key, sep, value = field.partition("=")
        if not sep:
            continue
        out[key.strip().lower()] = value.strip().lower()
    return out


def is_quiet(reading: dict | None) -> bool:
    """Should we hold our tongue?

    Two independent grounds, either sufficient. Neither is inferred from the
    absence of the other — an unreadable reading is not quiet, and a phone that
    never answered the DND question is not in DND.
    """
    if not reading:
        return False
    if reading.get("mode") in QUIET_MODES:
        return True
    granted = str(reading.get("granted", "")).strip()
    if granted not in ("1", "true", "yes"):
        return False            # the filter below was never really answered
    dnd = str(reading.get("dnd", "")).strip()
    return bool(dnd) and dnd not in (FILTER_ALL, "unknown")


def read(timeout: float = 2.0) -> dict | None:
    """Ask the companion once. None on anything that is not a reading."""
    endpoint = url()
    if not endpoint:
        return None
    try:
        with urllib.request.urlopen(endpoint, timeout=timeout) as resp:
            body = resp.read(256).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 — every failure is the same failure
        log.debug("ringer: %s did not answer: %s", endpoint, e)
        return None
    return parse(body)


def state_path() -> Path:
    """Where this service leaves what it knows, for `media doctor` to read."""
    return state_dir() / "ringer.json"


def snapshot(reading: dict | None, now: float | None = None) -> dict:
    """The published record: the reading, the verdict, and when it was taken.

    ``checked_at`` is wall-clock and not monotonic on purpose — it is read by
    another *process*, often on another *host*, and a monotonic clock means
    nothing to either. The reader compares it against its own wall clock, which
    is what makes a skewed or stopped publisher age out rather than persist.
    """
    now = time.time() if now is None else now
    quiet = is_quiet(reading)
    return {
        "quiet": quiet,
        "mode": (reading or {}).get("mode", "unknown"),
        "dnd": (reading or {}).get("dnd", "unknown"),
        "granted": (reading or {}).get("granted", "0"),
        "answered": reading is not None,
        "checked_at": now,
    }


def publish_file(snap: dict) -> None:
    """Leave the snapshot on disk for `media doctor` on this phone."""
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snap))
        tmp.replace(path)
    except OSError as e:
        log.warning("ringer: could not write %s: %s", path, e)


def publish_broker(snap: dict) -> bool:
    """Put the snapshot on the local speech broker, where the origin reads it.

    Local because this runs on the phone: the broker whose ``user-data`` the
    origin drives over the tcp bridge is this host's own sink-speech socket.
    Best-effort, like every other write to that socket — a broker that is down
    or too old to have ``user-data`` (mpv < 0.36) simply leaves the property
    absent, and absent reads as "speak".
    """
    from .sinks import speech as _speech

    try:
        return _speech.set_ringer(snap)
    except Exception as e:  # noqa: BLE001 — diagnostics, never this loop's problem
        log.debug("ringer: broker publish failed: %s", e)
        return False


def tick(previous: dict | None = None) -> dict:
    """One poll: read, publish, and say so when the verdict changed.

    Logged on the edge only. At this interval a line per poll is four thousand
    a day saying nothing, and the one thing anybody ever wants from this log is
    when the phone went quiet and when it came back.
    """
    snap = snapshot(read())
    publish_file(snap)
    publish_broker(snap)
    was = None if previous is None else previous.get("quiet")
    if was != snap["quiet"]:
        log.info("ringer: %s (mode=%s dnd=%s granted=%s) — alerts %s",
                 "quiet" if snap["quiet"] else "audible",
                 snap["mode"], snap["dnd"], snap["granted"],
                 "held" if snap["quiet"] else "speak")
    if not snap["answered"] and (previous is None or previous.get("answered")):
        # Once per outage, not once per poll. On a phone whose companion has
        # been killed this is the only notice that the gate has stopped
        # gating — and it must, because unanswered means audible.
        log.warning("ringer: %s is not answering — alerts will speak until "
                    "it does", url())
    return snap


def main(argv: "list[str] | None" = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    every = interval_s()
    log.info("ringer: asking %s every %.0fs", url(), every)
    previous: dict | None = None
    while True:
        previous = tick(previous)
        time.sleep(every)


if __name__ == "__main__":
    sys.exit(main())
