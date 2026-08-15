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
  * **pause speech/voice**;
  * speech does **not auto-resume** — you un-pause it manually (popup Space /
    ``media resume``) when the call is done.

**Music left this daemon on 2026-08-15.** The companion app now holds Android
audio focus on mpv's behalf and ducks the phone's music on any focus loss,
restoring it on the GAIN — which is the signal this whole file exists because
mpv ignores. Nothing here touches the music broker any more: not a duck, not a
pause. That is the first retirement the companion app's design predicted, and
the reason for it is not tidiness but correctness — three separate duckers on
one volume lost the restore between them on 2026-08-14 and left the music at 10
for two hours.

The duck machinery remains, unused by default, because retiring a workaround is
a claim about the device that a real call has not yet tested. See
``_DEFAULT_DUCK_SOCKET_NAMES`` for the one env line that puts it back.

For a **call**, the daemon leaves speech paused until you resume it, and only
*ducks* music — restoring its volume when the call ends. (The opt-in external
hold, below, additionally auto-resumes the paused speech.)

Two mechanisms hold the pause down for the whole call:

  * **poll re-assert** — every notification poll, while the call is active, we
    re-send pause to the sockets. This pauses audio already playing when the
    call starts (within one poll) and is a cheap backstop.
  * **event hold** — the reason a poll alone isn't enough: the speech sink
    clears pause/mute at the *start of each reply* (so a fresh reply is audible
    after an idle popup-pause), and ``play_playlist`` on the phone path does it
    unconditionally. So a TTS reply that *starts mid-call* un-pauses the broker
    itself. A flag the sink could check doesn't help here — the sink runs on
    the host driving the phone (red5), not on the phone, so it can't see
    phone-side call state without a cross-machine push and per-reply latency.
    Instead, for the duration of the call we hold an mpv ``observe_property``
    subscription on ``pause`` on each *local* broker socket; the instant
    anything un-pauses it, we re-pause over that same connection (~1 ms). A
    mid-call reply therefore loads paused and can't play over the conversation.

Once the call clears we stop both — leaving the broker paused for a manual
resume; we never resume it after a call.

**External hold (opt-in).** Any external trigger can pause+duck playback by
touching a flag file — ``media-call-guard --hold`` sets it, ``--release`` clears
it (or point ``MEDIA_CALL_GUARD_HOLD_FLAG`` elsewhere). Both the guard and the
trigger must resolve that variable *the same way*: the CLI reports success
whichever path it lands on, so a trigger running in an environment that does
not see the guard's setting (a non-interactive ssh, a cron job, a desktop
launcher) will write a flag nothing polls and duck nothing. The flag is
checked on a
fast tick (``MEDIA_CALL_GUARD_FLAG_POLL_S``, default 0.3s) — decoupled from the
slower call-notification poll — and *debounced*: it must be present for
``MEDIA_CALL_GUARD_HOLD_ENGAGE_S`` (default 1.5s) before engaging, so short
voice-typing utterances are ignored and only sustained dictation ducks, and
absent for ``MEDIA_CALL_GUARD_HOLD_RELEASE_S`` (default 2s) before releasing, so
the flag flicking between utterances doesn't drop the hold. On release it
**auto-resumes** (unlike a call). This is the durable half of a "duck while
Google voice typing is active" feature: an Automate/Tasker/MacroDroid macro
detects the mic recording and sets/clears the flag (confirmed working with
Automate's "Audio device recording" block). If a call overlaps a hold episode,
auto-resume is suppressed — the call's manual-resume policy wins.

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
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from ._paths import state_dir
from .sinks import _mpv_ipc as ipc

log = logging.getLogger(__name__)

# Command that lists active status-bar notifications as a JSON array. Termux:API
# ships it as `termux-notification-list`; overridable for testing / other hosts.
_DEFAULT_LIST_CMD = "termux-notification-list"

# Packages whose notification means "a call is happening". Two cover the common
# Android cases: the AOSP telephony ConnectionService posts under
# `com.android.server.telecom`, while the Google/Pixel dialer posts the
# incoming/outgoing/ongoing-call notification under `com.google.android.dialer`
# (confirmed on the target phone: an outgoing call showed pkg=dialer,
# content="Calling"). Both clear the notification when the call ends. Add others
# (e.g. `org.linphone` for SIP) via MEDIA_CALL_GUARD_PACKAGES.
_DEFAULT_PACKAGES = "com.android.server.telecom,com.google.android.dialer"

# A matched notification whose title/content matches this is NOT a live call.
# The dialer posts "Missed call" and "Voicemail" notifications that persist
# after the call ends; without this they'd read as an active call forever and
# wedge the detector (and hold audio paused).
_DEFAULT_EXCLUDE_RE = r"(?i)missed|voicemail"

# The phone-local mpv IPC sockets to pause: speech and the phone-local music
# bridge. Resolved under the agent-media state dir unless
# MEDIA_CALL_GUARD_SOCKETS overrides with an explicit path list.
#
# mpv-voice.sock was dropped 2026-08-13. It was the pre-sink-speech voice
# broker, superseded by the phase-2 cut-over (246fa60) and silent since
# 2026-05-07 -- retiring it was already on the plan in
# docs/reference/restructure.md. Probing a socket nothing plays to only cost a
# pointless connect on every call, and kept a dead lane looking load-bearing.
# mpv-music.sock was dropped 2026-08-15: **the companion app owns the music
# volume now.** It holds Android audio focus on mpv's behalf and ducks the
# phone's music on any focus loss, a call included, restoring it on the GAIN —
# the thing this daemon was written to fake because no such signal existed.
# Two duckers on one volume is not a redundancy, it is the bug: whoever captured
# the pre-duck level must be the one to put it back, and on 2026-08-14 three of
# them lost the restore between them and left the music at 10 for two hours.
#
# sink-book.sock was ADDED 2026-08-15, on the same argument that keeps speech
# here and for the same reason it pauses rather than ducks: a book is longform
# speech, and a half-heard sentence is a lost one that nothing replays. It is
# the strongest case of the three -- an audiobook runs for hours, so it is the
# channel most likely to be the thing playing when the mic opens or the phone
# rings, and the only one that will still be running twenty minutes later if
# nobody stops it. The companion app does not cover this: its focus policy
# ducks music and pauses speech, and the book broker's --ao=openal ignores
# Android audio focus like the others, so without a line here a call talks over
# Rothfuss indefinitely.
#
# Note the asymmetry with speech that this inherits: no auto-resume. The book
# stays paused until David lifts it, which for a book is the right default --
# it is a thing you come back to, and coming back to it is a deliberate act.
_DEFAULT_SOCKET_NAMES = ("sink-speech.sock", "sink-book.sock")

# Sockets to DUCK (lower volume) instead of pausing while a hold is active.
# Empty since 2026-08-15 — see above; nothing is ducked from here any more.
#
# The machinery stays, because retiring a workaround is a claim about the
# device: that a real call delivers a focus callback to the app. Focus ducking
# is proven on p8a for another app taking the output (2026-08-15 07:42:20, duck
# to 10 and back to 110 on the GAIN) but has NOT been seen for a call, which is
# by nature hard to rehearse. If a call turns out to arrive silently, one env
# line puts this back with no code change:
#
#     MEDIA_CALL_GUARD_DUCK_SOCKETS=$PREFIX/tmp/mpv-music.sock
#
# (duck_list is resolved independently of MEDIA_CALL_GUARD_SOCKETS, so that is
# the whole restoration.)
_DEFAULT_DUCK_SOCKET_NAMES = ()
_DEFAULT_DUCK_VOLUME = 20.0

_DEFAULT_POLL_S = 1.5

# External-hold flag file. Any external trigger (e.g. a Tasker/MacroDroid/Automate
# macro firing on voice-typing start/stop) touches this to pause + hold, and
# removes it to auto-resume. See `--hold` / `--release` and `_run_loop`.
_DEFAULT_HOLD_FLAG_NAME = "call-guard.hold"

# Where a running guard publishes the hold-flag path it is actually polling.
# Deliberately at the DEFAULT location and deliberately not overridable: it is
# the one file a trigger can find without already knowing the answer. A remote
# `ssh HOST media-call-guard --hold' sources no profile, so it cannot see a
# MEDIA_CALL_GUARD_HOLD_FLAG set in an env file that only the service manager
# reads — it would resolve the default path, write a flag nothing polls, and
# report success. Publishing the answer here is what lets it find out.
_FLAG_ADVERT_NAME = "call-guard.flag-path"

# Heartbeat for the thing that WRITES the flag. call_guard cannot tell a
# working mic-detect trigger from a dead one — both look like "no flag" — and
# that is exactly how barge-in stayed broken for two days in August 2026 while
# every service reported healthy. Touching this on each external hold turns a
# silent failure into an observable one: `media selfcheck` reports how long it
# has been quiet, and doctor complains once that exceeds a day.
# The input claim. Interval well under the TTL so two posts can be lost before
# the floor frees itself; see ClaimHeartbeat for why the ratio is the point.
_DEFAULT_CLAIM_OWNER = "cece"
_DEFAULT_CLAIM_TTL_S = 45.0
_DEFAULT_CLAIM_INTERVAL_S = 15.0
# Floor on the re-assert interval, so a misconfigured 0 becomes "once a second"
# rather than a spin against the endpoint. A module constant because the tests
# need sub-second heartbeats to observe several in a bounded run.
_CLAIM_MIN_INTERVAL_S = 1.0

_LAST_HOLD_NAME = "call-guard.last-hold"
# ...and the same for holds nobody typed, which is the only kind that proves
# the trigger still fires. See `note_external_hold`.
_LAST_EXTERNAL_NAME = "call-guard.last-external"

# The flag file is a cheap local stat, so we check it on a fast tick — decoupled
# from the (expensive) notification poll for calls, which stays at poll_s.
_DEFAULT_FLAG_POLL_S = 0.3
# Debounce for the external hold: the flag must be present continuously for
# ENGAGE_S before we duck/pause (so short voice-typing utterances are ignored —
# only sustained dictation triggers it), and absent for RELEASE_S before we
# release (so the flag flicking off between utterances doesn't drop the hold).
_DEFAULT_HOLD_ENGAGE_S = 1.5
_DEFAULT_HOLD_RELEASE_S = 2.0
# Backstop for a hold whose release never arrives. A caller that passes --ttl
# has said how long it means to hold and is already safe; the danger is every
# caller that does not — the Automate bridge (which deletes the flag when the
# mic stops, and cannot if it is killed mid-dictation) and the turn-taking
# hold/release pair, whose release travels over ssh to the phone and can be
# lost with the connection. Either way the flag stays, music stays ducked and
# speech stays paused for as long as the phone runs, with every health check
# reporting well — the failure is silent by construction.
#
# Well above any real hold: dictation and a spoken turn are minutes at most, so
# this releases nothing that was still wanted. It is a dead-man's switch, not a
# TTL. 0 disables it (a hold then lasts until --release, as it used to).
_DEFAULT_HOLD_MAX_S = 1800.0
# When to *report* a hold that is still in effect, so it is visible before the
# backstop fires 25 minutes later. Also the answer to "why is it quiet?".
_DEFAULT_HOLD_WARN_S = 300.0

_stop = False


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


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
        self.duck = _resolve_duck_sockets()
        # Sockets we pause vs. sockets we duck. A duck socket is never paused.
        self.pause_list = [s for s in self.sockets if s not in self.duck]
        self.duck_list = list(self.duck)
        self.duck_volume = _env_float(
            "MEDIA_CALL_GUARD_DUCK_VOLUME", _DEFAULT_DUCK_VOLUME)
        self.poll_s = _env_float("MEDIA_CALL_GUARD_POLL_S", _DEFAULT_POLL_S)
        self.flag_poll_s = _env_float(
            "MEDIA_CALL_GUARD_FLAG_POLL_S", _DEFAULT_FLAG_POLL_S)
        self.hold_engage_s = _env_float(
            "MEDIA_CALL_GUARD_HOLD_ENGAGE_S", _DEFAULT_HOLD_ENGAGE_S)
        self.hold_release_s = _env_float(
            "MEDIA_CALL_GUARD_HOLD_RELEASE_S", _DEFAULT_HOLD_RELEASE_S)
        self.hold_max_s = _env_float(
            "MEDIA_CALL_GUARD_HOLD_MAX_S", _DEFAULT_HOLD_MAX_S)
        self.hold_flag = os.environ.get(
            "MEDIA_CALL_GUARD_HOLD_FLAG",
            str(state_dir() / _DEFAULT_HOLD_FLAG_NAME))
        # The input claim (see ClaimHeartbeat). Off unless a URL is set, which
        # is the whole gate: a host with nothing to tell simply configures
        # nothing, and every other guard behaviour is untouched either way.
        self.claim_url = os.environ.get("MEDIA_INPUT_CLAIM_URL", "").strip()
        self.claim_owner = os.environ.get(
            "MEDIA_INPUT_CLAIM_OWNER", _DEFAULT_CLAIM_OWNER).strip()
        self.claim_ttl_s = _env_float(
            "MEDIA_INPUT_CLAIM_TTL_S", _DEFAULT_CLAIM_TTL_S)
        self.claim_interval_s = _env_float(
            "MEDIA_INPUT_CLAIM_INTERVAL_S", _DEFAULT_CLAIM_INTERVAL_S)
        # Optional shell commands fired on the *call* rising/falling edge (not
        # the flag hold). Lets a phone's call detection reach across the tailnet
        # to duck another host's media.
        #
        # Name the remote's flag path explicitly. A non-interactive ssh sources
        # no profile, so if the far end sets MEDIA_CALL_GUARD_HOLD_FLAG in an
        # env file that only its service manager reads, a bare
        # `ssh HOST media-call-guard --hold` resolves the *default* path
        # instead: it writes a flag that host's guard never polls, prints
        # "hold flag set", and ducks nothing. Two paths, one watched, and the
        # failure is silent in the direction that matters.
        #
        #   F=/storage/emulated/0/agent-media/call-guard.hold  # the far end's
        #   MEDIA_CALL_GUARD_CALL_ENGAGE_CMD="ssh sp4 env MEDIA_CALL_GUARD_HOLD_FLAG=$F media-call-guard --hold"
        #   MEDIA_CALL_GUARD_CALL_RELEASE_CMD="ssh sp4 env MEDIA_CALL_GUARD_HOLD_FLAG=$F media-call-guard --release"
        #
        # Best-effort and fire-and-forget: a failing hook never wedges the guard
        # — which is also why a wrong path here never announces itself.
        self.call_engage_cmd = os.environ.get(
            "MEDIA_CALL_GUARD_CALL_ENGAGE_CMD", "").strip()
        self.call_release_cmd = os.environ.get(
            "MEDIA_CALL_GUARD_CALL_RELEASE_CMD", "").strip()


def _resolve_sockets() -> list[str]:
    raw = os.environ.get("MEDIA_CALL_GUARD_SOCKETS", "")
    if raw.strip():
        return [s.strip() for s in raw.split(",") if s.strip()]
    st = state_dir()
    return [str(st / name) for name in _DEFAULT_SOCKET_NAMES]


def _resolve_duck_sockets() -> set[str]:
    # An explicit env value (even "") wins, so it can disable ducking entirely.
    raw = os.environ.get("MEDIA_CALL_GUARD_DUCK_SOCKETS")
    if raw is not None:
        return {s.strip() for s in raw.split(",") if s.strip()}
    st = state_dir()
    return {str(st / name) for name in _DEFAULT_DUCK_SOCKET_NAMES}


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
    Duck sockets are skipped here — they're ducked, not paused.
    """
    for sock in cfg.pause_list:
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


def resume_sockets(cfg: Config, dry_run: bool = False) -> None:
    """Best-effort un-pause of the paused brokers. Used only by the external-hold
    auto-resume — never after a call. The caller releases the event hold *before*
    calling this so the un-pause isn't instantly re-held."""
    for sock in cfg.pause_list:
        if not os.path.exists(sock):
            continue
        if dry_run:
            log.info("[dry-run] would resume %s", sock)
            continue
        try:
            ipc.set_property(sock, "pause", False)
            log.info("resumed %s", sock)
        except (ipc.MpvIpcError, OSError) as e:
            log.warning("could not resume %s: %s", sock, e)


def duck_sockets(cfg: Config, saved: dict, dry_run: bool = False,
                 quiet: bool = False) -> None:
    """Lower the volume of each duck socket to ``cfg.duck_volume``, remembering
    its original volume in ``saved`` (once, on the first duck) so it can be
    restored later. A socket is only ducked after its baseline volume is read,
    so ``unduck_sockets`` always has a value to put back."""
    for sock in cfg.duck_list:
        if not os.path.exists(sock):
            continue
        if dry_run:
            if not quiet:
                log.info("[dry-run] would duck %s to %g", sock, cfg.duck_volume)
            continue
        try:
            if sock not in saved:
                vol = ipc.get_property(sock, "volume")
                if not isinstance(vol, (int, float)):
                    continue  # can't establish a baseline — retry next cycle
                saved[sock] = float(vol)
            ipc.set_property(sock, "volume", cfg.duck_volume)
            if not quiet:
                log.info("ducked %s (%g -> %g)",
                         sock, saved[sock], cfg.duck_volume)
        except (ipc.MpvIpcError, OSError) as e:
            log.warning("could not duck %s: %s", sock, e)


def unduck_sockets(cfg: Config, saved: dict, dry_run: bool = False) -> None:
    """Restore each ducked socket to its saved original volume, then forget it.
    Always safe to call (it only sets volume, never starts playback), so it runs
    on every hold release — after a call as well as an external hold."""
    for sock, vol in list(saved.items()):
        if dry_run:
            log.info("[dry-run] would restore volume on %s", sock)
            saved.pop(sock, None)
            continue
        try:
            if os.path.exists(sock):
                ipc.set_property(sock, "volume", vol)
                log.info("restored %s volume -> %g", sock, vol)
        except (ipc.MpvIpcError, OSError) as e:
            log.warning("could not restore volume on %s: %s", sock, e)
        saved.pop(sock, None)


# One-line mpv IPC commands the event-hold sends on its persistent connection.
_OBSERVE_PAUSE = (json.dumps({"command": ["observe_property", 1, "pause"]})
                  + "\n").encode()
_REPAUSE = (json.dumps({"command": ["set_property", "pause", True]})
            + "\n").encode()

# How long a hold worker blocks in recv before checking its stop flag, and how
# long it backs off before reconnecting a dropped/absent socket.
_HOLD_RECV_TIMEOUT = 0.5
_HOLD_RECONNECT_S = 0.5


def _is_unpause_event(msg: object) -> bool:
    """True for an mpv ``pause`` property-change to False (i.e. an un-pause).

    mpv also emits the *current* value right after ``observe_property``, so this
    fires on connect too if the broker is already un-paused — which is what we
    want mid-call: catch a reply that started before the hold attached.
    """
    return (isinstance(msg, dict)
            and msg.get("event") == "property-change"
            and msg.get("name") == "pause"
            and msg.get("data") is False)


def _hold_worker(sock_path: str, stop: threading.Event) -> None:
    """Keep `sock_path` paused for as long as `stop` is unset.

    Holds a persistent connection with an ``observe_property pause``
    subscription; on any un-pause (including the initial value on connect),
    re-pauses over the same socket. Reconnects if the broker restarts or the
    socket isn't up yet. All errors are swallowed — a hold failure must never
    take the guard down; the poll re-assert still covers it.
    """
    while not stop.is_set() and not _stop:
        try:
            s = ipc._open(sock_path, _HOLD_RECV_TIMEOUT)
        except (ipc.MpvIpcError, OSError):
            stop.wait(_HOLD_RECONNECT_S)
            continue
        try:
            s.sendall(_OBSERVE_PAUSE)
            buf = b""
            while not stop.is_set() and not _stop:
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break  # broker closed the socket — reconnect
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line.decode())
                    except ValueError:
                        continue
                    if _is_unpause_event(msg):
                        s.sendall(_REPAUSE)
        except OSError:
            pass  # transport hiccup — fall through to reconnect
        finally:
            try:
                s.close()
            except OSError:
                pass
        if not stop.is_set():
            stop.wait(_HOLD_RECONNECT_S)


class PauseHold:
    """Manages the per-socket event-hold threads for the length of a call."""

    def __init__(self, sockets: list[str]) -> None:
        self._sockets = sockets
        self._stop: threading.Event | None = None
        self._threads: list[threading.Thread] = []

    @property
    def active(self) -> bool:
        return self._stop is not None

    def start(self) -> None:
        if self.active:
            return
        self._stop = threading.Event()
        for sock in self._sockets:
            t = threading.Thread(
                target=_hold_worker, args=(sock, self._stop),
                name=f"call-hold:{os.path.basename(sock)}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("event-hold engaged on %d socket(s)", len(self._threads))

    def stop(self) -> None:
        if not self.active:
            return
        assert self._stop is not None
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []
        self._stop = None
        log.info("event-hold released")


class ClaimHeartbeat:
    """Tell red5 that David's input is spoken for, while the mic is hot.

    A live Claude session (cece) runs in Anthropic's cloud with the phone as
    its microphone, so it has no process on red5 to hold the converse
    rendezvous socket open — the mechanism that makes a *local* claim safe has
    nothing to attach to. The hot mic is the only observable proof the session
    exists, and this daemon is already watching it: the same flag that drives
    the duck drives the claim.

    ## Why a heartbeat and not an engage/release pair

    The obvious design is one POST on the rising edge and a DELETE on the
    falling one. That is the shape of the flag contract right above, and it is
    exactly the shape that needs `MEDIA_CALL_GUARD_HOLD_MAX_S` to survive,
    because the *release* is the half that goes missing — this process is
    killed, the phone drops off the tailnet, Android reaps the app.

    A claim has no such half. It carries its own expiry and is re-asserted
    while it remains true, so stopping IS the release, and a holder that dies
    frees the floor by falling silent. There is no state left behind to expire
    and no deadlock to engineer around, which is worth more here than the
    handful of requests it costs.

    The interval must stay well under the TTL — at the defaults, two posts can
    be lost before the floor frees. The ratio is the point, not the numbers.

    ## Failure is not this daemon's problem

    Every error is logged and dropped. The claim is an optimisation on top of a
    system that already worked: a missed one costs an overlap, which is the
    status quo, while anything that let a failed POST disturb the duck would
    trade a real feature for a speculative one. This runs on its own thread for
    the same reason — the guard's loop is a fast local tick and must never wait
    on a network round trip.
    """

    def __init__(self, url: str, owner: str, ttl_s: float,
                 interval_s: float) -> None:
        self._url = url
        self._owner = owner
        self._ttl_s = ttl_s
        # A heartbeat at or above the TTL cannot keep a claim alive; clamp
        # rather than refuse, since a misconfigured interval should degrade to
        # "claims a bit more often" and never to "silently claims nothing".
        ceiling = max(_CLAIM_MIN_INTERVAL_S, ttl_s / 2)
        self._interval_s = max(_CLAIM_MIN_INTERVAL_S,
                               min(interval_s, ceiling))
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        return self._stop is not None

    def _post_once(self) -> bool:
        body = json.dumps({
            "owner": self._owner,
            "ttl_s": self._ttl_s,
            "source": "phone-mic",
        }).encode()
        req = urllib.request.Request(
            self._url, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return 200 <= resp.status < 300
        except Exception as e:  # noqa: BLE001 — see the class docstring
            log.warning("input claim: %s", e)
            return False

    def _worker(self, stop: threading.Event) -> None:
        # Post immediately: the first claim is the one that matters, because
        # the collision it prevents happens at the start of the conversation.
        while True:
            self._post_once()
            if stop.wait(self._interval_s):
                return

    def start(self) -> None:
        if self.active or not self._url:
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._worker, args=(self._stop,),
            name="input-claim", daemon=True)
        self._thread.start()
        log.info("input claim: holding for %s (ttl %.0fs, every %.0fs)",
                 self._owner, self._ttl_s, self._interval_s)

    def stop(self) -> None:
        if not self.active:
            return
        assert self._stop is not None
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._stop = None
        # Deliberately no DELETE. The claim expires on its own, and a release
        # that can fail is worse than one that cannot — see the class docstring.
        log.info("input claim: released (expires within %.0fs)", self._ttl_s)


def _flag_ttl(path: str) -> float | None:
    """Seconds this hold is good for, or None when it never expires.

    Carried in the flag file's own contents (``ttl=120``) rather than in the
    guard's environment, for two reasons. It is per-caller: the Automate
    mic-detect bridge writes an empty flag and must keep its current
    behaviour — dictation lasts as long as it lasts, and a TTL that released
    mid-sentence would be a regression. And it needs no restart: a new caller
    opts in by writing a different file, not by reconfiguring a running
    service.
    """
    try:
        with open(path) as fh:
            body = fh.read(64)
    except OSError:
        return None
    for line in body.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "ttl":
            try:
                seconds = float(value.strip())
            except ValueError:
                return None
            return seconds if seconds > 0 else None
    return None


def flag_present(cfg: Config) -> bool:
    """Whether the external-hold flag is present AND still valid.

    An expired flag is deleted, not merely ignored. A trigger that fires
    ``--hold`` and then dies — a chat closed mid-conversation, a crashed
    caller — otherwise leaves music quiet indefinitely, and the whole point of
    a TTL is that nobody has to notice. Deleting it also means the ordinary
    release path runs: absence for the debounce window, then auto-resume.

    A hold with no TTL of its own gets the backstop instead, because the
    callers that most need one are exactly the callers that pass no TTL. Most
    of a hold's lifetime it makes no difference: the backstop is half an hour
    and no real hold comes close.
    """
    try:
        stat = os.stat(cfg.hold_flag)
    except OSError:
        return False

    # time.time(), NOT _now(). _now() is time.monotonic() — seconds since
    # boot — and st_mtime is epoch seconds, so subtracting one from the other
    # yields a large negative number and the hold never expires. That shipped
    # once; the live test caught it and the unit test did not, because the test
    # patched _now() and so encoded the same wrong assumption as the code.
    ttl = _flag_ttl(cfg.hold_flag)
    backstop = ttl is None
    if backstop:
        ttl = cfg.hold_max_s if cfg.hold_max_s > 0 else None
    if ttl is not None and (time.time() - stat.st_mtime) > ttl:
        if backstop:
            log.warning(
                "external hold has stood for %.0fs with no release (src=%s) — "
                "releasing; whatever held it is presumed gone",
                ttl, _flag_source(cfg.hold_flag) or "external")
        else:
            log.info(
                "external hold expired after %.0fs without a heartbeat — releasing",
                ttl)
        try:
            os.unlink(cfg.hold_flag)
        except OSError:                          # pragma: no cover - racing release
            pass
        return False
    return True


def _flag_source(path: str) -> str:
    """Who wrote this hold flag, from its contents (``src=cli``), or "".

    The heartbeat records only that *something* held, and two very different
    things write the same flag: the Automate mic-detect bridge, and a person or
    script running ``--hold`` (the Sam/Cece turn-taking does exactly that). So
    "the trigger fired yesterday" could mean mic-detect is alive or that someone
    typed a command, and the file could not tell them apart — which is the whole
    question the heartbeat exists to answer.
    """
    try:
        with open(path) as fh:
            body = fh.read(64)
    except OSError:
        return ""
    for line in body.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "src":
            return value.strip()[:16]
    return ""


def _set_hold(cfg: Config, ttl: float | None = None, source: str = "cli") -> None:
    p = Path(cfg.hold_flag)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Rewriting rather than touching is what makes a heartbeat work: mtime is
    # the clock the TTL is measured against, so re-running --hold extends the
    # hold instead of starting a second one.
    body = f"ttl={ttl:g}\n" if ttl else ""
    if source:
        body += f"src={source}\n"
    p.write_text(body)


def _clear_hold(cfg: Config) -> None:
    try:
        Path(cfg.hold_flag).unlink()
    except FileNotFoundError:
        pass


def advert_path() -> Path:
    """Where the running guard publishes the flag path it polls."""
    return state_dir() / _FLAG_ADVERT_NAME


def last_hold_path() -> Path:
    """Where the guard records the most recent EXTERNAL hold."""
    return state_dir() / _LAST_HOLD_NAME


def last_external_hold_path() -> Path:
    """Heartbeat for holds that only the *trigger* could have caused."""
    return state_dir() / _LAST_EXTERNAL_NAME


def note_external_hold(source: str = "") -> None:
    """Timestamp an external hold, best-effort.

    mtime is the timestamp; the contents name the source, because a hold from
    the mic-detect bridge and a hold someone typed are the same event here and
    only one of them answers "is barge-in still working". An unlabelled flag —
    what the Automate bridge writes — is recorded as "external": honest about
    being un-attributed rather than guessing at the only writer we know of.

    A second file records only the un-typed ones. The health check reads that,
    because a `--hold` someone ran is not evidence the trigger is alive — and
    silencing the alarm for a day is exactly what such a hold used to do. Found
    the hard way: a hold written by hand to prove the *receiving* half worked
    reset the clock on the alarm that had just caught the sending half dead.
    """
    try:
        p = last_hold_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{source or 'external'}\n")
        if (source or "external") != "cli":
            last_external_hold_path().write_text(f"{source or 'external'}\n")
    except OSError as exc:                      # pragma: no cover - unwritable
        log.debug("could not record last hold: %s", exc)


def hold_age(cfg: Config) -> float | None:
    """Seconds the external hold has been in effect, or None when not held.

    Reads the flag itself rather than any state the guard keeps in memory, so a
    health check can answer "why has it gone quiet?" without the guard's help —
    including when the guard is the thing that died holding it.
    """
    try:
        return max(0.0, time.time() - os.stat(cfg.hold_flag).st_mtime)
    except OSError:
        return None


def hold_warn_s() -> float:
    """How long a hold may stand before it is worth reporting. 0 disables."""
    return _env_float("MEDIA_CALL_GUARD_HOLD_WARN_S", _DEFAULT_HOLD_WARN_S)


def last_hold_source() -> str:
    """Who last held, per note_external_hold. "" when there is no record."""
    try:
        return last_hold_path().read_text().strip()[:16]
    except OSError:
        return ""


def publish_flag_path(cfg: Config) -> None:
    """Advertise the flag path this guard polls, for triggers that can't know it.

    Best-effort: a guard that cannot write the advert must still guard.
    """
    try:
        p = advert_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(cfg.hold_flag + "\n")
    except OSError as exc:                      # pragma: no cover - unwritable state dir
        log.warning("could not publish flag path: %s", exc)


def unpublish_flag_path() -> None:
    """Remove the advert on a clean stop, so nothing follows a dead guard."""
    try:
        advert_path().unlink()
    except (FileNotFoundError, OSError):
        pass


def advertised_flag_path() -> str | None:
    """The flag path a running guard published, or None if there is no advert."""
    try:
        value = advert_path().read_text().strip()
    except OSError:
        return None
    return value or None


def resolve_trigger_flag(cfg: Config) -> str:
    """The flag path a --hold/--release should write, and why.

    Explicit beats discovery: an operator who set MEDIA_CALL_GUARD_HOLD_FLAG
    gets exactly that, because overriding it is how you drive a guard that is
    not running yet. But a mismatch with a live guard is worth saying out loud
    — it is precisely the case that used to duck nothing and report success.
    """
    explicit = os.environ.get("MEDIA_CALL_GUARD_HOLD_FLAG")
    advertised = advertised_flag_path()
    if explicit:
        if advertised and advertised != explicit:
            print(f"warning: the running guard polls {advertised}, "
                  f"not {explicit} — this hold will not reach it",
                  file=sys.stderr)
        return explicit
    if advertised and advertised != cfg.hold_flag:
        print(f"note: following the running guard's flag path: {advertised}",
              file=sys.stderr)
        return advertised
    return cfg.hold_flag


def _hold_reason(call: bool, flag: bool) -> str:
    if call and flag:
        return "call + external hold"
    return "call" if call else "external hold"


def _now() -> float:
    return time.monotonic()


class FlagHold:
    """Debounced external-hold state derived from the flag file.

    Engages only after the flag has been present continuously for ``engage_s``
    — so short voice-typing utterances (which flick the flag briefly) are
    ignored and only sustained dictation ducks — and releases only after it's
    been absent for ``release_s``, so the flag flicking off between the
    utterances of one long message doesn't drop the hold. ``update`` is called
    every fast tick with the current flag state and a monotonic timestamp.
    """

    def __init__(self, engage_s: float, release_s: float) -> None:
        self.engage_s = engage_s
        self.release_s = release_s
        self._held = False
        self._present_since: float | None = None
        self._absent_since: float | None = None

    def update(self, flag_now: bool, now: float) -> bool:
        if flag_now:
            self._absent_since = None
            if self._present_since is None:
                self._present_since = now
            if not self._held and now - self._present_since >= self.engage_s:
                self._held = True
        else:
            self._present_since = None
            if self._absent_since is None:
                self._absent_since = now
            if self._held and now - self._absent_since >= self.release_s:
                self._held = False
        return self._held


def _run_call_hook(cmd: str, label: str, dry_run: bool = False) -> None:
    """Fire a call-edge hook command, best-effort. Runs in its own thread with a
    short timeout so a slow/hanging command (e.g. an SSH that stalls) never
    delays the guard's own pause. Failures are logged, never raised."""
    if not cmd:
        return
    if dry_run:
        log.info("[dry-run] would run %s hook: %s", label, cmd)
        return

    def _work() -> None:
        try:
            subprocess.run(cmd, shell=True, timeout=15.0,
                           capture_output=True, text=True)
            log.info("call %s hook fired", label)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("call %s hook failed: %s", label, e)

    threading.Thread(target=_work, daemon=True).start()


def _run_loop(cfg: Config, dry_run: bool = False) -> None:
    hold = PauseHold(cfg.pause_list)  # event-hold only guards the *paused* sockets
    flaghold = FlagHold(cfg.hold_engage_s, cfg.hold_release_s)
    # A dry run must not tell red5 anything: an empty URL makes start() a
    # no-op, so the claim goes inert without threading dry_run through it.
    claim = ClaimHeartbeat("" if dry_run else cfg.claim_url, cfg.claim_owner,
                           cfg.claim_ttl_s, cfg.claim_interval_s)
    prev_flag = False        # last cycle's flag state, for the claim edges
    prev_want = False        # was anything holding the pause last cycle?
    prev_call = False        # last cycle's call state, for firing edge hooks
    call_in_episode = False  # did a call participate in the current hold?
    last_call = False        # last known call state (kept across query failures)
    ducked: dict = {}        # duck socket -> its pre-duck volume, for restoring
    tick = cfg.flag_poll_s or _DEFAULT_FLAG_POLL_S
    # The flag is a cheap local stat (checked every fast tick); notifications
    # (for calls) are an expensive subprocess, so poll them only every ~poll_s.
    notif_every = max(1, round(cfg.poll_s / tick))
    i = 0
    while not _stop:
        if i % notif_every == 0:
            notifs = list_notifications(cfg)
            # On a transient query failure keep the previous reading, not flap.
            if notifs is not None:
                last_call = call_active(notifs, cfg)
        call = last_call
        if call and not prev_call:
            _run_call_hook(cfg.call_engage_cmd, "engage", dry_run=dry_run)
        elif prev_call and not call:
            _run_call_hook(cfg.call_release_cmd, "release", dry_run=dry_run)
        prev_call = call
        flag = flaghold.update(flag_present(cfg), _now())

        # Claimed on the FLAG edge, not on `want`. Two reasons to keep this
        # separate from the duck below: `want` is also raised by a call, and a
        # call is not cece — claiming her name for it would tell red5 something
        # untrue. And `want`'s rising edge only fires once, so a flag arriving
        # during a call would never start the heartbeat at all.
        if flag and not prev_flag:
            claim.start()
        elif prev_flag and not flag:
            claim.stop()
        prev_flag = flag

        want = call or flag

        if want:
            if call:
                call_in_episode = True
            if not prev_want:
                log.info("pausing/ducking phone audio (%s)",
                         _hold_reason(call, flag))
                if flag:
                    # Only the flag path proves the external trigger is alive;
                    # a phone call would tick this without mic-detect running.
                    note_external_hold(_flag_source(cfg.hold_flag))
                if not dry_run:
                    hold.start()  # instant re-pause on any un-pause
                pause_sockets(cfg, dry_run=dry_run, quiet=False)
                duck_sockets(cfg, ducked, dry_run=dry_run, quiet=False)
            elif i % notif_every == 0:
                # Periodic re-assert (backstop) at the slow cadence — not every
                # fast tick. Catches audio that started playing mid-hold.
                pause_sockets(cfg, dry_run=dry_run, quiet=True)
                duck_sockets(cfg, ducked, dry_run=dry_run, quiet=True)
        elif prev_want:
            # Every hold reason cleared. Release the event hold, then restore.
            hold.stop()
            # Music volume is always restored (un-ducking never starts playback).
            unduck_sockets(cfg, ducked, dry_run=dry_run)
            if call_in_episode:
                # A call was involved — speech keeps the manual-resume policy.
                log.info("hold cleared — speech left paused (no auto-resume "
                         "after a call); music volume restored")
            else:
                log.info("external hold released — resuming")
                resume_sockets(cfg, dry_run=dry_run)
            call_in_episode = False

        prev_want = want
        i += 1
        time.sleep(tick)
    hold.stop()  # on SIGTERM / shutdown
    claim.stop()  # stop re-asserting; the claim itself ages out on red5
    unduck_sockets(cfg, ducked, dry_run=dry_run)  # don't leave music ducked


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
    parser.add_argument("--hold", action="store_true",
                        help="set the external-hold flag (pause + hold now) and exit")
    parser.add_argument("--ttl", type=float, metavar="SECONDS",
                        help="with --hold: release automatically after SECONDS unless "
                             "another --hold arrives first. Re-run --hold --ttl to "
                             "heartbeat. Without it a hold lasts until --release, which "
                             "is right for a trigger that reliably fires both edges and "
                             "wrong for one that can die mid-conversation.")
    parser.add_argument("--release", action="store_true",
                        help="clear the external-hold flag (auto-resume) and exit")
    parser.add_argument("--source", default="cli", metavar="NAME",
                        help="with --hold: who is holding, recorded in the "
                             "heartbeat (default: cli). The mic-detect health "
                             "check reports it, so a hold you typed is not "
                             "mistaken for proof the trigger is alive.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    cfg = Config()

    if args.hold or args.release:
        cfg.hold_flag = resolve_trigger_flag(cfg)
    if args.hold:
        _set_hold(cfg, args.ttl, source=args.source)
        suffix = f" (expires in {args.ttl:g}s unless refreshed)" if args.ttl else ""
        print(f"hold flag set: {cfg.hold_flag}{suffix}")
        return 0
    if args.release:
        _clear_hold(cfg)
        print(f"hold flag cleared: {cfg.hold_flag}")
        return 0
    if args.probe is not None:
        return _probe(cfg, args.probe)

    log.info("call_guard starting: packages=%s pause=%s duck=%s@vol%g "
             "poll=%.1fs flag_poll=%.1fs engage=%.1fs release=%.1fs "
             "hold_flag=%s%s",
             sorted(cfg.packages), cfg.pause_list, cfg.duck_list,
             cfg.duck_volume, cfg.poll_s, cfg.flag_poll_s, cfg.hold_engage_s,
             cfg.hold_release_s, cfg.hold_flag,
             " [dry-run]" if args.dry_run else "")
    publish_flag_path(cfg)
    try:
        _run_loop(cfg, dry_run=args.dry_run)
    finally:
        unpublish_flag_path()
    log.info("call_guard stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
