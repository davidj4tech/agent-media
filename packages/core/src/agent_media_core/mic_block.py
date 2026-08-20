"""Keep a background recogniser's microphone block applied.

## Why anything has to

The companion tells a person from a probe by how long the microphone is held
(``MicSteady``), and on p8a that only works while
``com.google.android.as`` is blocked from ``RECORD_AUDIO``. Blocked, Android
opens its session and feeds it zeros; a silenced recording counts for nothing
on its own, while still counting as *company* for a real Gboard dictation, so
both answers stay right.

Unblocked, the same recogniser holds the microphone for ten, twenty, twenty-two
seconds at a time, every half minute, around the clock. Nothing in the public
API says who is recording, so each one reads as David dictating and pauses the
reply he is listening to.

## Why it is a service and not a note in a runbook

The block does not stick. Measured on p8a: set at 18:2x on 2026-08-19, gone by
22:10; re-applied at 10:26 on 2026-08-20, gone by 12:35. Something in the
platform — a role re-grant, an update, an unused-app sweep — puts it back to
``foreground``, silently, hours later.

Twice now the way we found out was David saying speech was broken, and twice
the whole stack was healthy and behaving exactly as designed. A setting that
reverts on its own every few hours is not a decision anyone can make once; it
is a state something has to hold.

So this holds it, and logs every revert it repairs — which is also the only
measurement of how fast the platform undoes it that anyone has.

## Deliberately opt-in

Naming a package here silences an app's microphone. That is David's decision on
his phone (taken 2026-08-19), not a default any install should inherit, so the
service does nothing at all unless ``MEDIA_MIC_BLOCK_PACKAGES`` names
something. An empty setting is not a misconfiguration; it is the normal state
of every other host.

## Needs a shell

``appops`` is not available to an ordinary app or to Termux's uid, so this
drives it through ``adb`` — which on p8a talks to the phone's own adbd over
loopback (Wireless debugging, self-paired). When adb cannot be reached the tick
says so and the loop keeps going: the block may still be in force, and a phone
that has dropped its pairing is a thing to report, not to crash over.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import time

log = logging.getLogger(__name__)

#: How often to look. The revert has never been observed inside an hour, so
#: this is not a race — it bounds how long a reverted block can go unnoticed,
#: and costs one cheap loopback command.
DEFAULT_INTERVAL_S = 300.0

#: The mode we want. `ignore` makes Android open the recording and feed it
#: zeros, which is what marks it `silenced` for everyone watching.
BLOCKED = "ignore"

# `appops get` answers with the uid mode first and the package's own op after:
#
#   Uid mode: RECORD_AUDIO: ignore
#   RECORD_AUDIO: allow; time=+9m43s ago; rejectTime=+2h5m ago; duration=+22s
#
# The uid line is the one that decides — a `set` without `--uid` writes the
# second line and is overridden by the first, which is how an apparently
# successful re-apply changed nothing on 2026-08-20.
_UID_MODE = re.compile(r"^\s*Uid mode:\s*RECORD_AUDIO:\s*(\S+)", re.MULTILINE)


def packages() -> list[str]:
    """Packages to keep blocked. Empty unless someone asked for this."""
    raw = os.environ.get("MEDIA_MIC_BLOCK_PACKAGES", "")
    return [p.strip() for p in raw.replace(",", " ").split() if p.strip()]


def interval_s() -> float:
    try:
        return max(30.0, float(os.environ.get("MEDIA_MIC_BLOCK_INTERVAL_S",
                                              DEFAULT_INTERVAL_S)))
    except ValueError:
        return DEFAULT_INTERVAL_S


def parse_uid_mode(text: str) -> str | None:
    """The uid-level mode from `appops get` output, or None if it is not there.

    None means "could not tell", never "not blocked": a phone that answered
    something unexpected must not be treated as one that answered `allow`, or
    every failure to read becomes a write.
    """
    match = _UID_MODE.search(text or "")
    return match.group(1) if match else None


def _adb(args: list[str], timeout: float = 20.0) -> tuple[int, str]:
    """Run an adb command, returning (rc, output). Never raises."""
    if shutil.which("adb") is None:
        return 127, "adb not installed"
    try:
        done = subprocess.run(["adb", *args], capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def read_mode(package: str) -> str | None:
    rc, out = _adb(["shell", "appops", "get", package, "RECORD_AUDIO"])
    if rc != 0:
        return None
    return parse_uid_mode(out)


def apply_block(package: str) -> bool:
    """Set the uid-level op. True when the phone confirms it took."""
    rc, _ = _adb(["shell", "appops", "set", "--uid", package,
                  "RECORD_AUDIO", BLOCKED])
    if rc != 0:
        return False
    return read_mode(package) == BLOCKED


def tick(package: str, last_applied: float | None,
         now: float | None = None) -> tuple[str, float | None]:
    """One check. Returns (what happened, when the block was last applied).

    The outcomes are deliberately distinct in the log, because they mean very
    different things: `held` is the quiet normal, `reverted` is the platform
    undoing a decision and is worth a line every time, and `unreadable` is a
    phone we have lost our shell on — which is not evidence either way about
    the block.
    """
    now = time.time() if now is None else now
    mode = read_mode(package)
    if mode is None:
        log.warning("mic-block: cannot read %s's RECORD_AUDIO op — adb "
                    "unreachable? leaving whatever is in force", package)
        return "unreadable", last_applied
    if mode == BLOCKED:
        return "held", last_applied
    stood = ("" if last_applied is None
             else f" after {(now - last_applied) / 60:.0f}m")
    if apply_block(package):
        log.warning("mic-block: %s's mic block had reverted to %r%s — "
                    "re-applied. Until now every reply was being paused by "
                    "its recogniser holding the mic", package, mode, stood)
        return "reverted", now
    log.error("mic-block: %s is %r and the re-apply did not take", package, mode)
    return "failed", last_applied


def main(argv: "list[str] | None" = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    names = packages()
    if not names:
        # Not an error, and not a reason to crash-loop: this is what every host
        # that has not opted in looks like.
        log.info("mic-block: MEDIA_MIC_BLOCK_PACKAGES is empty — nothing to "
                 "hold; sleeping")
        while True:
            time.sleep(3600)
    every = interval_s()
    log.info("mic-block: holding RECORD_AUDIO=%s for %s, every %.0fs",
             BLOCKED, ", ".join(names), every)
    applied: dict[str, float | None] = {n: None for n in names}
    while True:
        for name in names:
            _, applied[name] = tick(name, applied[name])
        time.sleep(every)


if __name__ == "__main__":
    sys.exit(main())
