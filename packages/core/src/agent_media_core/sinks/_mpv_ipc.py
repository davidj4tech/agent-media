"""Minimal mpv JSON-IPC client over a Unix socket — or a TCP bridge.

Synchronous, one-shot per call. mpv replies with one JSON line per
command on the same socket; we read until newline or timeout.

An endpoint is normally a Unix-socket path (str/Path). It may also be a
`tcp://host:port` string, which connects over TCP instead — used to reach a
*remote* mpv whose IPC socket has been bridged to a TCP port (e.g. the phone's
mpv-music exposed over Tailscale via socat). The line-delimited JSON protocol
is identical over either transport, so every helper below works unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

log = logging.getLogger(__name__)


class MpvIpcError(RuntimeError):
    pass


_TCP_PREFIX = "tcp://"


# --- slow-endpoint breaker -------------------------------------------------
#
# A tcp:// bridge is only as good as the link under it. The phone bridge can sit
# at 400ms RTT with 25% loss, where each one-shot call costs ~2s (connect, send,
# recv — three round-trips, times up to 3 retries). A single spoken sentence
# makes a dozen such calls to check and duck what's playing, so speech ended up
# waiting ~24s on a device that wasn't going to answer usefully anyway.
#
# So: when a tcp endpoint answers slowly or fails, stop calling it for a while.
# Every one-shot call site already treats MpvIpcError as "unknown, carry on"
# (they catch and pass, or return None/False), so failing fast degrades exactly
# the way an unreachable bridge already does — just immediately instead of
# eventually. Unix-socket endpoints are never breakered: they run at ~2ms and a
# local sink-speech stall is a real error worth surfacing.

_NS = "mpv"
_breaker_until: dict[str, float] | None = None      # lazy; unix-epoch deadlines


def _state() -> dict[str, float]:
    """Breaker deadlines, loaded once per process from the shared store."""
    global _breaker_until
    if _breaker_until is None:
        from .. import _breaker
        _breaker_until = _breaker.load(_NS)
    return _breaker_until


def _slow_s() -> float:
    try:
        return float(os.environ.get("MEDIA_MPV_SLOW_MS", "1200")) / 1000
    except ValueError:
        return 1.2


def _breaker_s() -> float:
    """How long to skip a slow endpoint. 0 disables the breaker entirely."""
    try:
        return float(os.environ.get("MEDIA_MPV_BREAKER_S", "20"))
    except ValueError:
        return 20.0


def _is_remote(endpoint: str | Path) -> bool:
    return str(endpoint).startswith(_TCP_PREFIX)


def _guard(endpoint: str | Path, critical: bool = False) -> None:
    """Raise immediately if `endpoint` is in its cool-off window.

    `critical=True` bypasses the skip: the breaker exists to stop *policy*
    chatter (is anything playing? duck it) from delaying speech, and every such
    call site treats failure as "unknown, carry on". Delivering the audio is a
    different matter — skipping that doesn't degrade speech, it silences it. So
    playback commands always attempt, however slow the endpoint is.

    They no longer breaker the endpoint on latency, though (see `_record`). A
    call that exempts itself from the skip and then sets a deadline can only
    ever penalise the calls that don't exempt themselves — here, the display
    read. One `pause` keypress at 5s on a 450ms-RTT link would blank the popup
    for the whole cool-off, so the control the user just pressed worked and the
    screen sat unchanged, which is indistinguishable from a control that didn't.
    """
    if critical or not _is_remote(endpoint):
        return
    until = _state().get(str(endpoint))
    if until and time.time() < until:
        raise MpvIpcError(
            f"{endpoint}: skipped, endpoint slow "
            f"({until - time.time():.0f}s left)")


def _record(endpoint: str | Path, elapsed: float, failed: bool,
            slow_s: float | None = None,
            breaker_s: float | None = None) -> None:
    """Open the breaker when a remote call was slow or failed; close on a fast one.

    `slow_s` overrides the latency budget for this one call; `0` means latency
    never trips the breaker, only outright failure does. That is the right rule
    for a call whose *only* cost is its own staleness — a display read. The
    default budget is tuned to keep policy chatter from delaying speech, and by
    that standard every honest round trip to a phone on the far side of the
    world is "slow": ~2s. Judging a display read by it means the breaker is open
    almost permanently and a short utterance's few seconds of playback are never
    once observed, so the popup reads blank. Failure still trips it, so an
    unreachable bridge doesn't make every redraw wait out the timeout.

    `breaker_s` overrides how long this call's failure keeps the endpoint shut.
    The default window is sized for policy chatter, where being wrong costs one
    unducked track; for a display read it is the length of time the screen lies
    about what is playing. This link drops a fifth of its packets, so a read
    fails now and then with nothing wrong at the far end — and a 45s penalty for
    one lost packet leaves the popup blank far more often than not.
    """
    if not _is_remote(endpoint):
        return
    key = str(endpoint)
    window = _breaker_s() if breaker_s is None else breaker_s
    if window <= 0:
        return
    limit = _slow_s() if slow_s is None else slow_s
    state = _state()
    was_open = key in state
    if failed or (limit > 0 and elapsed >= limit):
        state[key] = time.time() + window
    elif was_open:
        state.pop(key, None)
    else:
        return                                   # nothing changed; no write
    from .. import _breaker
    _breaker.store(_NS, state)


def reset_breaker(endpoint: str | Path | None = None) -> None:
    """Forget breaker state — for tests, and for an explicit user retry."""
    from .. import _breaker
    state = _state()
    if endpoint is None:
        state.clear()
        _breaker.clear(_NS)
    else:
        state.pop(str(endpoint), None)
        _breaker.store(_NS, state)


def _open(endpoint: str | Path, timeout: float) -> socket.socket:
    """Connect to an mpv IPC endpoint (Unix path or `tcp://host:port`)."""
    ep = str(endpoint)
    if ep.startswith(_TCP_PREFIX):
        hostport = ep[len(_TCP_PREFIX):]
        host, _, port = hostport.rpartition(":")
        if not host or not port:
            raise MpvIpcError(f"bad tcp endpoint {ep!r} (want tcp://host:port)")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, int(port)))
        return s
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(ep)
    return s


def _send(sock_path: str | Path, command: list[Any], timeout: float = 5.0,
          critical: bool = False) -> dict:
    _guard(sock_path, critical)
    t0 = time.monotonic()
    failed = True
    try:
        reply = _send_inner(sock_path, command, timeout)
        failed = False
        return reply
    finally:
        _record(sock_path, time.monotonic() - t0, failed,
                slow_s=0 if critical else None)


def _send_inner(sock_path: str | Path, command: list[Any], timeout: float = 5.0) -> dict:
    s = _open(sock_path, timeout)
    try:
        s.sendall((json.dumps({"command": command}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        line = buf.split(b"\n", 1)[0]
        if not line:
            raise MpvIpcError("empty reply")
        return json.loads(line.decode())
    finally:
        s.close()


def command(sock_path: str | Path, *args: Any, timeout: float = 5.0,
            critical: bool = False) -> Any:
    """Send `command` with positional args. Returns `data` from the reply,
    or raises MpvIpcError on non-success.

    Over a tcp:// bridge (socat fork-per-connection to a remote mpv), a single
    request can transiently fail or come back a spurious "property unavailable"
    under rapid polling — which would cut a clip short (idle() reads "done") or
    drop a loadfile (clip skipped). So retry a few times for tcp endpoints. All
    the commands we send (loadfile replace / set_property / get_property) are
    idempotent, so a retry can't double-apply. Unix-socket calls stay single-shot.
    """
    attempts = 3 if str(sock_path).startswith(_TCP_PREFIX) else 1
    last: Exception = MpvIpcError("unreached")
    for attempt in range(attempts):
        try:
            reply = _send(sock_path, list(args), timeout=timeout,
                          critical=critical)
            if reply.get("error", "success") != "success":
                raise MpvIpcError(f"{args[0]}: {reply.get('error')}")
            return reply.get("data")
        except (MpvIpcError, OSError) as e:
            last = e
            if attempt + 1 < attempts:
                time.sleep(0.04)
    raise last


def command_batch(sock_path: str | Path, commands: list, timeout: float = 5.0,
                  critical: bool = False) -> None:
    """Send several mpv commands over ONE connection instead of a fresh connect
    per command.

    Over a tcp:// bridge each connect is a full Germany→AU round-trip (~600ms),
    so issuing a playlist load as ~10 separate commands costs seconds. Pipelining
    them on one socket collapses that to a single round-trip. Fire-and-forget: the
    bytes are flushed to mpv and we drain briefly so it has processed before we
    close; loadfile/set_property are idempotent, so we don't match every reply.
    Retries once for tcp endpoints on a transport error.
    """
    _guard(sock_path, critical)
    attempts = 3 if str(sock_path).startswith(_TCP_PREFIX) else 1
    last: Exception = MpvIpcError("unreached")
    payload = b"".join((json.dumps({"command": list(c)}) + "\n").encode()
                       for c in commands)
    t0 = time.monotonic()
    for attempt in range(attempts):
        try:
            s = _open(sock_path, timeout)
            try:
                s.sendall(payload)
                s.settimeout(0.3)  # let mpv process before we close the socket
                try:
                    while s.recv(4096):
                        pass
                except socket.timeout:
                    pass
            finally:
                s.close()
            _record(sock_path, time.monotonic() - t0, False,
                    slow_s=0 if critical else None)
            return
        except OSError as e:
            last = e
            if attempt + 1 < attempts:
                time.sleep(0.04)
    _record(sock_path, time.monotonic() - t0, True,
            slow_s=0 if critical else None)
    raise last


def send_nowait(sock_path: str | Path, *args: Any, timeout: float = 3.0,
                critical: bool = False) -> None:
    """Send one command without waiting for the reply.

    For a remote control action the caller often doesn't need the "ok" — the new
    state is mirrored/confirmed elsewhere — and waiting for mpv to finish adds
    real latency: pausing suspends the phone's audio device (~0.6s round-trip),
    whereas the connect+send itself is ~0.3s. Connect, send, close: the bytes are
    flushed before the FIN, so socat still forwards the command to mpv even
    though we never read the reply. Transport errors raise (the caller can fall
    back to a waited `set_property`).

    Not waited for, but read — on a thread, after this has returned.
    Discarding the reply entirely made a *refusal* indistinguishable from
    success: the player answered `{"error":"invalid parameter"}` to every
    press of a key whose verb it did not implement, nothing was listening, and
    the key silently did nothing. That was pause, for as long as the phone
    lane ended at an app answering a subset of mpv's verbs; then seek, for as
    long again. Both were found by accident. A refusal now lands in
    `media errors`, which costs the presser nothing and turns a dead control
    into a line someone can read.
    """
    _guard(sock_path, critical)
    t0 = time.monotonic()
    failed = True
    try:
        s = _open(sock_path, timeout)
        sent = False
        try:
            s.sendall((json.dumps({"command": list(args)}) + "\n").encode())
            failed = False
            sent = True
        finally:
            if sent:
                threading.Thread(target=_read_refusal, args=(s, list(args)),
                                 daemon=True).start()
            else:
                s.close()
    finally:
        _record(sock_path, time.monotonic() - t0, failed,
                slow_s=0 if critical else None)


#: How long the background reader waits for the answer to a fire-and-forget
#: command. Long enough for a player that is going to refuse — it refuses
#: immediately, without touching the audio device — and short enough that the
#: thread cannot outlive the press it is reading about.
REFUSAL_READ_S = 2.0


def _read_refusal(sock: socket.socket, command: list) -> None:
    """Read one reply; record it if the player refused. Never raises."""
    try:
        sock.settimeout(REFUSAL_READ_S)
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return
            buf += chunk
        for line in buf.split(b"\n"):
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            # An mpv connection also carries async events; ours is the one
            # that answers, and only a reply has `error`.
            if not isinstance(msg, dict) or "error" not in msg:
                continue
            err = msg.get("error")
            if err and err != "success":
                verb = str(command[0]) if command else "?"
                log.warning("mpv refused %s: %s", verb, err)
                _log_refusal(verb, str(err), command)
            return
    except (OSError, ValueError):
        return
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _log_refusal(verb: str, err: str, command: list) -> None:
    """Put it where `media errors` will show it.

    Best-effort by design: a control that worked must not fail because we
    could not write down that it did.
    """
    try:
        from ..state import StateStore

        StateStore().log_error(
            "mpv", f"player refused {verb!r}: {err}",
            extras={"command": [str(c) for c in command]})
    except Exception:  # noqa: BLE001
        pass


def event_stream(sock_path: str | Path,
                 heartbeat: float = 1.0) -> Iterator[Optional[dict]]:
    """Yield mpv async event dicts from a *persistent* connection.

    mpv pushes async events (`start-file`, `end-file`, `idle`, ...) to every
    connected IPC client, interleaved with command replies. This opens one
    long-lived connection and yields only the `event` messages (dropping
    command replies). It yields `None` every `heartbeat` seconds of silence
    so a caller can check a stop flag without blocking forever, and returns
    (generator exhausts) when the socket closes — e.g. the broker exits.

    Distinct from `_send`, which is one-shot per call; don't mix the two on
    the same connection.
    """
    s = _open(sock_path, heartbeat)
    try:
        buf = b""
        while True:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                yield None
                continue
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line.decode())
                except ValueError:
                    continue
                if isinstance(msg, dict) and "event" in msg:
                    yield msg
    finally:
        s.close()


def get_property(sock_path: str | Path, name: str, timeout: float = 2.0,
                 critical: bool = False) -> Any:
    # `command` already retries transient failures for tcp:// (bridge) endpoints.
    #
    # `critical` is for a read a *control* depends on — the volume a nudge is
    # relative to, the playlist-pos a skip steps from. Skipping those doesn't
    # save any chatter, it just makes the control compute from a wrong default
    # (volume snapping to 100, a sentence skip degrading to a time-seek). Like
    # every critical call it never trips the breaker on latency, only failure.
    return command(sock_path, "get_property", name, timeout=timeout,
                   critical=critical)


def get_properties(sock_path: str | Path, names: list,
                   timeout: float = 2.0, attempts: int = 1,
                   slow_s: float | None = None,
                   breaker_s: float | None = None) -> dict:
    """Fetch several properties over ONE connection (request_id-matched).

    A monitor that reads playlist-pos + idle + pause + time-pos every tick would
    otherwise pay a separate ~600ms bridge round-trip *per* property. Pipelining
    them on one socket makes the whole snapshot one round-trip. Returns
    ``{name: value}`` for the ones that answered success; missing/errored names
    are simply absent (callers treat that as unknown). Async events are ignored.

    ``attempts``: transport retries. A round where *nothing* answered — connect
    refused, or every reply eaten (a tcp bridge drop; a dozing phone whose
    radio takes seconds to wake) — is a transport failure and, with attempts
    > 1, retried. A round where ANY request was answered is authoritative:
    missing names are then real per-property errors, not transport loss. The
    default stays single-shot so per-tick pollers (replay tracker, popup
    status) can't stall their cadence on a dead endpoint; user-initiated
    actions (the chapter browser) pass a generous count instead. Exhaustion
    re-raises the last connect error, or returns {} when connections opened
    but nothing answered (the pre-retry semantics).

    ``slow_s`` / ``breaker_s``: per-call breaker policy (see `_record`). A read
    that only displays something passes 0 and a short window, so neither
    ordinary bridge latency nor one lost packet shuts it out of the endpoint
    whose state it is trying to show.
    """
    _guard(sock_path)
    t0 = time.monotonic()
    ok = False
    try:
        last_err: Optional[OSError] = None
        for attempt in range(max(1, attempts)):
            if attempt:
                time.sleep(0.2)
            try:
                out, answered = _get_properties_once(sock_path, names, timeout)
            except OSError as e:
                last_err = e
                continue
            if answered:
                ok = True
                return out
        if last_err is not None:
            raise last_err
        return {}
    finally:
        _record(sock_path, time.monotonic() - t0, not ok, slow_s=slow_s,
                breaker_s=breaker_s)


def display_properties(sock_path: str | Path, names: list,
                       timeout: float = 2.0) -> dict:
    """A snapshot whose only purpose is to be shown to someone.

    The breaker's defaults are sized for policy chatter — "is anything playing,
    should I duck it" — where being skipped costs one unducked track and being
    slow delays speech. A read that feeds a status line or a progress bar is
    the opposite case on every axis, and judging it by those defaults is what
    left the popup blank while audio was plainly coming out of the phone:

    - latency is not a fault. Every honest answer from a phone on the far side
      of the world takes ~2s, which the default budget calls slow, so the
      breaker sat open permanently on the endpoint the display depends on.
    - one lost packet is not an outage. This link drops a fifth of them, and a
      single-shot read that fails costs the full cool-off — reads every second,
      blanked for forty-five.
    - a stale display is the whole cost of being wrong, so the cool-off should
      be short: long enough to stop hammering a phone that is genuinely gone,
      short enough that recovery is not something you wait for.

    Failure still opens the breaker, briefly, so a dead endpoint doesn't make
    every redraw wait out the connect timeout.
    """
    return get_properties(sock_path, names, timeout=timeout, attempts=2,
                          slow_s=0, breaker_s=5)


def _get_properties_once(sock_path: str | Path, names: list,
                         timeout: float) -> tuple[dict, int]:
    """One transport round of `get_properties`: ({answered-ok}, #answered)."""
    idx = {i + 1: n for i, n in enumerate(names)}
    s = _open(sock_path, timeout)
    try:
        payload = b"".join(
            (json.dumps({"command": ["get_property", n], "request_id": i + 1})
             + "\n").encode()
            for i, n in enumerate(names))
        s.sendall(payload)
        out: dict = {}
        answered: set = set()
        s.settimeout(timeout)
        buf = b""
        deadline = time.time() + timeout
        # Stop as soon as every request has been *answered* — success OR error.
        # An idle mpv replies "property unavailable" for time-pos/duration, which
        # never land in `out`; keying the exit on len(out) would then spin until
        # the timeout on every idle snapshot (2s per popup/status-bar redraw).
        while len(answered) < len(names) and time.time() < deadline:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line.decode())
                except ValueError:
                    continue
                rid = msg.get("request_id")
                if rid in idx and "error" in msg:  # a command reply, not an event
                    answered.add(rid)
                    if msg.get("error") == "success":
                        out[idx[rid]] = msg.get("data")
        return out, len(answered)
    finally:
        s.close()


def set_property(sock_path: str | Path, name: str, value: Any,
                 critical: bool = False) -> None:
    command(sock_path, "set_property", name, value, critical=critical)
