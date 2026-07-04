"""call_guard: pause the phone's local audio when a phone call arrives.

The phone's mpv brokers run with ``--ao=openal`` / ``--ao=opensles`` (chosen
because they survive Bluetooth route changes on Termux). A side effect is that
they ignore Android *audio focus* — so an incoming call plays TTS/music
straight over the ringtone and then over the conversation, instead of ducking
like a well-behaved media app. This daemon closes that gap: it watches for a
call and pauses the phone's local mpv brokers the instant one starts.

Detection is non-root. A stock, unrooted Termux app can't read telephony state
(`dumpsys`/`su` are unavailable, and `termux-telephony-deviceinfo` doesn't
report call state), so the one live signal left is the *notification* the
dialer/telephony service posts for an incoming/ongoing call. We poll
``termux-notification-list`` (Termux:API) for it. That requires **Notification
Access** granted to Termux:API once (Android Settings → Notification access →
enable "Termux:API").

Policy (chosen 2026-07-04):
  * pause on the **ring** — the rising edge of a call, so you can still hear it;
  * pause **speech + phone-local music** (the local mpv sockets);
  * **do not auto-resume** — you un-pause manually (popup Space / ``media
    resume``) when the call is done.

So this daemon only ever *pauses*, and never issues a resume of its own.

It keeps *re-asserting* the pause on every poll for as long as the call lasts —
not just once on the ring. That's deliberate: the speech sink clears pause/mute
at the start of each response (so a fresh reply is audible after an idle
popup-pause), and ``play_playlist`` on the phone path does it unconditionally.
So a TTS reply that *starts mid-call* would un-pause itself and play over the
conversation; re-asserting every cycle re-pauses it within one poll instead
(its first fraction of a second may still leak — closing that fully would mean
gating the sink's pause-clear on call state). Once the call clears we stop
holding the pause but leave it paused — you resume manually.

Invoked via the ``media-call-guard`` console script; supervised as the
``call-guard`` runit service on the phone. Use ``media-call-guard --probe`` to
dump raw notifications (to calibrate the match against your dialer), or
``--dry-run`` to log what it *would* pause without touching playback.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import time

from ._paths import state_dir
from .sinks import _mpv_ipc as ipc

log = logging.getLogger(__name__)

# Command that lists active status-bar notifications as a JSON array. Termux:API
# ships it as `termux-notification-list`; overridable for testing / other hosts.
_DEFAULT_LIST_CMD = "termux-notification-list"

# Packages whose notification means "a call is happening". The AOSP/Pixel
# telephony ConnectionService posts the incoming/ongoing-call notification under
# `com.android.server.telecom` and clears it when the call ends — the cleanest
# signal. Dialer packages also post *missed-call* notifications that linger, so
# they're not in the default set; add them via MEDIA_CALL_GUARD_PACKAGES only
# together with the "missed" exclusion below.
_DEFAULT_PACKAGES = "com.android.server.telecom"

# A matched notification whose title/content matches this is NOT a live call
# (drops "Missed call" notifications, which persist after the call ends and
# would otherwise wedge the rising-edge detector).
_DEFAULT_EXCLUDE_RE = r"(?i)missed"

# The three phone-local mpv IPC sockets to pause: speech, the local voice
# broker, and the phone-local music bridge. Resolved under the agent-media state
# dir unless MEDIA_CALL_GUARD_SOCKETS overrides with an explicit path list.
_DEFAULT_SOCKET_NAMES = ("sink-speech.sock", "mpv-voice.sock", "mpv-music.sock")

_DEFAULT_POLL_S = 1.5

_stop = False


def _on_signal(_signum, _frame):
    global _stop
    _stop = True


class Config:
    """Resolved runtime configuration, from the environment."""

    def __init__(self) -> None:
        self.list_cmd = os.environ.get(
            "MEDIA_CALL_GUARD_LIST_CMD", _DEFAULT_LIST_CMD)
        self.packages = {
            p.strip() for p in os.environ.get(
                "MEDIA_CALL_GUARD_PACKAGES", _DEFAULT_PACKAGES).split(",")
            if p.strip()
        }
        inc = os.environ.get("MEDIA_CALL_GUARD_INCLUDE_RE", "")
        exc = os.environ.get("MEDIA_CALL_GUARD_EXCLUDE_RE", _DEFAULT_EXCLUDE_RE)
        self.include_re = re.compile(inc) if inc else None
        self.exclude_re = re.compile(exc) if exc else None
        self.sockets = _resolve_sockets()
        try:
            self.poll_s = float(os.environ.get(
                "MEDIA_CALL_GUARD_POLL_S", _DEFAULT_POLL_S))
        except ValueError:
            self.poll_s = _DEFAULT_POLL_S


def _resolve_sockets() -> list[str]:
    raw = os.environ.get("MEDIA_CALL_GUARD_SOCKETS", "")
    if raw.strip():
        return [s.strip() for s in raw.split(",") if s.strip()]
    st = state_dir()
    return [str(st / name) for name in _DEFAULT_SOCKET_NAMES]


def list_notifications(cfg: Config) -> list[dict] | None:
    """Active status-bar notifications as a list of dicts.

    Returns ``[]`` when there are none and ``None`` when the query failed
    (Termux:API missing, notification access not granted, timeout, bad JSON) —
    the caller treats ``None`` as "unknown this cycle" and does nothing, so a
    transient hiccup never spuriously re-arms or double-pauses.
    """
    try:
        r = subprocess.run(
            cfg.list_cmd.split(),
            capture_output=True, text=True, timeout=10.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def _matches(notif: dict, cfg: Config) -> bool:
    if notif.get("packageName") not in cfg.packages:
        return False
    text = f"{notif.get('title', '')}\n{notif.get('content', '')}"
    if cfg.exclude_re and cfg.exclude_re.search(text):
        return False
    if cfg.include_re and not cfg.include_re.search(text):
        return False
    return True


def call_active(notifs: list[dict], cfg: Config) -> bool:
    """True if any notification looks like a live (incoming/ongoing) call."""
    return any(_matches(n, cfg) for n in notifs)


def pause_sockets(cfg: Config, dry_run: bool = False, quiet: bool = False) -> None:
    """Best-effort pause of every configured mpv broker. A missing/idle socket
    is skipped silently — pausing is a safe no-op on a broker playing nothing.

    ``quiet`` drops the per-socket log line; the loop sets it on the repeated
    re-assert cycles so only the initial pause (and any error) is logged.
    """
    for sock in cfg.sockets:
        if not os.path.exists(sock):
            log.debug("socket absent, skipping: %s", sock)
            continue
        if dry_run:
            if not quiet:
                log.info("[dry-run] would pause %s", sock)
            continue
        try:
            ipc.set_property(sock, "pause", True)
            if not quiet:
                log.info("paused %s", sock)
        except (ipc.MpvIpcError, OSError) as e:
            log.warning("could not pause %s: %s", sock, e)


def _run_loop(cfg: Config, dry_run: bool = False) -> None:
    was_active = False  # whether the previous cycle saw a call
    while not _stop:
        notifs = list_notifications(cfg)
        if notifs is None:
            time.sleep(cfg.poll_s)  # unknown this cycle — leave state untouched
            continue
        active = call_active(notifs, cfg)
        if active:
            if not was_active:
                log.info("call detected — pausing phone audio")
            # Re-assert every cycle: a reply starting mid-call clears its own
            # pause, so hold it down until the call ends. Log only the first.
            pause_sockets(cfg, dry_run=dry_run, quiet=was_active)
        elif was_active:
            log.info("call cleared — stopped holding pause (no auto-resume)")
        was_active = active
        time.sleep(cfg.poll_s)


def _probe(cfg: Config, seconds: float) -> int:
    """Dump raw notifications for `seconds` so the call match can be calibrated
    against the phone's actual dialer. Place/receive a call while it runs."""
    deadline = time.time() + seconds if seconds > 0 else float("inf")
    while not _stop and time.time() < deadline:
        notifs = list_notifications(cfg)
        if notifs is None:
            print("!! termux-notification-list failed "
                  "(is Notification Access granted to Termux:API?)")
        else:
            print(f"-- {len(notifs)} notification(s), "
                  f"call_active={call_active(notifs, cfg)}")
            for n in notifs:
                print(f"   pkg={n.get('packageName')!r} "
                      f"title={n.get('title')!r} content={n.get('content')!r}")
        time.sleep(cfg.poll_s)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="media-call-guard")
    parser.add_argument("--probe", nargs="?", type=float, const=60.0,
                        metavar="SECONDS",
                        help="dump raw notifications (default 60s) to calibrate "
                             "the call match, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="log what would be paused without touching playback")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    cfg = Config()

    if args.probe is not None:
        return _probe(cfg, args.probe)

    log.info("call_guard starting: packages=%s sockets=%s poll=%.1fs%s",
             sorted(cfg.packages), cfg.sockets, cfg.poll_s,
             " [dry-run]" if args.dry_run else "")
    _run_loop(cfg, dry_run=args.dry_run)
    log.info("call_guard stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
