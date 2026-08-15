"""Tiny HTTP endpoint for cross-host speech coordination on the tailnet.

Two jobs, both about a host that is not this one:

  GET  /speech        {"playing": bool} — reads the speech channel's mpv IPC
                      socket (sink-speech.sock). Used by remote duckers (e.g.
                      the hpo SMTC helper) to pause local media while speech is
                      talking. Book/music ride other sockets, so this is
                      speech-specific.

  POST /input-claim   a remote observer claiming the listener's next utterance,
                      because a live assistant session owns the mic on some
                      other device and has no process here to hold the converse
                      rendezvous open. See `capture/input_claim.py` for the full
                      reasoning. DELETE releases, GET inspects.

The input-claim half lives here rather than in its own service because the
transport is already right: a claim from a directly-connected peer lands in well
under a second, against the 5-10s a relay poll costs. That latency *is* the
problem being solved; it is the window in which this host talks over the
conversation.

## This used to be an untracked script, and that mattered

It lived at ``~/.local/bin/speech-state-server.py``, ran on the *system* python,
and therefore could not import `input_claim` — so the claim file's path, its TTL
default and its read/write/expiry rules were all duplicated here as literals and
kept in step by hand. The old docstring argued that duplication was deliberate.
It was only ever forced: the module is in the package now, imports the real
thing, and the second copy is gone. The one rule worth keeping from that era is
why the path was never made configurable — two parties resolving a configurable
path differently both report success while reading and writing different files,
and that failure is silent.

Binding is peer-network-only with no auth, the same trust boundary GET /speech
already assumed. The blast radius of a forged claim is a held speech channel
that expires by itself within the TTL.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote

from .._paths import state_dir
from ..capture import input_claim


DEFAULT_PORT = 8675

# ~15s of grace for tailscaled to come up before settling for loopback.
_BIND_RETRIES = 6
_BIND_RETRY_S = 2.5

# Longer than this is a bug or a stuck re-assert, not a real intention.
MAX_TTL_S = 600.0


def speech_socket() -> str:
    """The speech channel's mpv IPC socket.

    Via `state_dir()` rather than a literal so Termux's proot/native HOME split
    resolves the same way it does everywhere else in the package — the old
    standalone script hardcoded the proot-side path and would have opened a
    different file when run natively.
    """
    return str(state_dir() / "sink-speech.sock")


def speech_playing() -> bool:
    """True if the speech mpv is actively playing (core-idle == False)."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(speech_socket())
        s.sendall(b'{"command":["get_property","core-idle"]}\n')
        data = b""
        # mpv may emit unsolicited events; scan for our request_id==0 reply.
        while True:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
            for line in data.decode(errors="ignore").splitlines():
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("request_id") == 0 and o.get("error") == "success":
                    s.close()
                    return not bool(o.get("data"))
        s.close()
    except Exception:  # noqa: BLE001
        pass
    # Fail open: never leave a remote video stuck paused if we can't tell.
    return False


def media_bin() -> str | None:
    """Path to the `media` console script, or None if it cannot be found.

    Resolved from *this* interpreter first: a console script and the module it
    imports come from the same venv, so `sys.executable`'s directory is the one
    place the answer is certainly right. PATH is the fallback for an unusual
    install layout. The old script hardcoded one developer's checkout, which is
    exactly the kind of line that makes a package undistributable.
    """
    candidate = Path(sys.executable).with_name("media")
    if candidate.exists():
        return str(candidate)
    return shutil.which("media")


def speech_hold(owner: str, seconds: float | None = None,
                release: bool = False) -> None:
    """Drive the existing per-owner speech hold. Best-effort, never blocks.

    Deliberately reuses `media speech-hold` rather than gating speech here: the
    marker files under speech-hold.d/ are already the authority, already
    per-owner, already TTL'd, and already consulted at the start of every clip.
    A second gate answering the same question is a second thing to disagree.

    Still a detached subprocess even though this process *could* now import
    `set_speech_hold` directly. The hold publishes to the relay's floor mirror,
    and while that publish is best-effort it is not guaranteed non-blocking;
    keeping it out of the HTTP handler preserves the latency guarantee this
    endpoint exists to provide. A failure here leaves speech un-held, which is
    the status quo.
    """
    media = media_bin()
    if media is None:
        return
    argv = [media, "speech-hold", "--owner", owner]
    argv += ["--release"] if release else [str(int(seconds or 0))]
    try:
        subprocess.Popen(argv, start_new_session=True,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError:
        pass


class Handler(http.server.BaseHTTPRequestHandler):
    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:      # noqa: N802
        path = self.path.rstrip("/")
        if path in ("/speech", ""):
            self._json({"playing": speech_playing()})
            return
        if path == "/input-claim":
            cur = input_claim.held()
            self._json({"held": cur is not None, "claim": cur})
            return
        self.send_error(404)

    def do_POST(self) -> None:     # noqa: N802
        if self.path.rstrip("/") != "/input-claim":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
        except Exception as e:  # noqa: BLE001
            self._json({"ok": False, "error": f"bad body: {e}"}, 400)
            return

        owner = str(body.get("owner") or "").strip()
        if not owner:
            self._json({"ok": False, "error": "owner required"}, 400)
            return
        try:
            ttl = float(body.get("ttl_s", input_claim.DEFAULT_TTL_S))
        except (TypeError, ValueError):
            self._json({"ok": False, "error": "ttl_s must be a number"}, 400)
            return
        # Clamped, not rejected: a caller asking for an implausible window is
        # still telling us something real is happening, and refusing it
        # outright would drop a claim we could have honoured for a while.
        ttl = max(1.0, min(ttl, MAX_TTL_S))

        try:
            input_claim.claim(owner, ttl, str(body.get("source") or ""))
        except OSError as e:
            self._json({"ok": False, "error": str(e)}, 500)
            return
        # The hold rides slightly past the claim so a re-assert that arrives a
        # beat late does not open a gap the speech path can slip a clip into.
        speech_hold(owner, ttl * 1.5)
        self._json({"ok": True, "owner": owner, "ttl_s": ttl})

    def do_DELETE(self) -> None:   # noqa: N802
        raw, _, query = self.path.partition("?")
        if raw.rstrip("/") != "/input-claim":
            self.send_error(404)
            return
        owner = None
        for part in query.split("&"):
            if part.startswith("owner="):
                owner = unquote(part[6:]) or None
        # `release(owner)` is a no-op when someone else now holds the claim, so
        # read back rather than assume: the hold must only be lifted when the
        # claim actually went away, or a late DELETE from a finished session
        # un-holds speech for the session that replaced it.
        before = input_claim.held()
        input_claim.release(owner)
        cleared = before is not None and input_claim.held() is None
        if cleared and owner:
            speech_hold(owner, release=True)
        self._json({"ok": True, "cleared": cleared})

    def log_message(self, *a) -> None:
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _peer_ip() -> str | None:
    """This host's address on the peer network (Tailscale), if it has one."""
    ts = shutil.which("tailscale")
    if ts is None:
        return None
    try:
        out = subprocess.run([ts, "ip", "-4"], capture_output=True, text=True,
                             timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def resolve_bind(explicit: str | None = None) -> str:
    """Which address to listen on, in order of decreasing certainty.

    1. ``--bind``
    2. ``BIND`` — what the existing systemd drop-in sets from ``TS_IP``
    3. ``MEDIA_SPEECH_STATE_BIND``
    4. this host's Tailscale address, asked for at start-up
    5. ``127.0.0.1``

    The loopback fallback is the important one. This file used to default to a
    literal ``100.94.154.59`` — one specific machine's tailnet address, on a
    host that was not even the one it ran on; the unit's drop-in had been
    quietly correcting it. A wrong literal fails as "Name or service not known"
    on someone else's machine, which reads as a broken install. Loopback always
    binds: the endpoint works for local callers and is simply not reachable
    across the network until someone says where to listen, which is the honest
    default for a host we know nothing about.
    """
    for value in (explicit,
                  os.environ.get("BIND"),
                  os.environ.get("MEDIA_SPEECH_STATE_BIND")):
        if value and value.strip():
            return value.strip()

    # Retry before giving up on the peer network. At boot this can easily start
    # before tailscaled has an address, and the fallback is sticky: we bind once
    # and never look again, so losing the race means the endpoint is
    # loopback-only until something restarts it. The old unit solved this with
    # `After=tailnet-ip.service` — a dependency on a unit that exists on
    # exactly one machine, which is no use to anyone installing this fresh.
    for attempt in range(_BIND_RETRIES):
        ip = _peer_ip()
        if ip:
            return ip
        if attempt + 1 < _BIND_RETRIES:
            time.sleep(_BIND_RETRY_S)
    return "127.0.0.1"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="media-speech-state",
        description="HTTP endpoint for cross-host speech coordination.")
    ap.add_argument("--bind", help="address to listen on (default: this "
                                   "host's Tailscale address, else 127.0.0.1)")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT") or DEFAULT_PORT))
    a = ap.parse_args(argv)

    bind = resolve_bind(a.bind)
    try:
        server = Server((bind, a.port), Handler)
    except OSError as e:
        print(f"media-speech-state: cannot bind {bind}:{a.port} — {e}",
              file=sys.stderr)
        return 1
    print(f"media-speech-state: listening on {bind}:{a.port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
