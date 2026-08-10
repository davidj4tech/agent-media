#!/data/data/com.termux/files/usr/bin/env python3
"""Speak-on-this-device over HTTP, so the caller never needs ssh.

Why this exists: the link to this phone stalls without erroring — a radio doze
or a tailnet path change leaves an established connection hanging. ssh only
notices after ServerAliveInterval x ServerAliveCountMax, and with multiplexing
those belong to the *master*, so per-command timeouts a caller passes are
inert. A stalled utterance therefore sat in a dead socket for ninety seconds.
Measured: a 2-second clip that normally takes 6s took 104.5s.

curl bounds itself. `--max-time` and `--connect-timeout` are the caller's, not
a shared master's, and every request is its own connection with no state to go
stale between them.

The wire contract is deliberately identical to the ssh one, so agent-media's
remote-say needs no code change — only a different command:

    text on stdin  ->  POST /say
    CLIP <name>    ->  streamed back on stdout as it happens
    DURATION <s>
    exit non-zero  ->  HTTP 4xx/5xx (curl --fail)

Rendering itself is NOT reimplemented here. say.sh stays the one
implementation and this shells out to it, because two copies of the render
path is exactly how the last outage happened.
"""

from __future__ import annotations

import http.server
import os
import shutil
import socket
import subprocess
import sys
import threading

HOME = os.path.expanduser("~")
SAY_SH = os.environ.get("AM_SAY_SH") or next(
    (p for p in (f"{HOME}/projects/agent-media/deploy/phone/say.sh",
                 f"{HOME}/say.sh") if os.path.exists(p)), f"{HOME}/say.sh")
TOKEN = os.environ.get("AM_SAY_HTTP_TOKEN", "")
PORT = int(os.environ.get("AM_SAY_HTTP_PORT", "8790"))
MAX_BODY = int(os.environ.get("AM_SAY_HTTP_MAX_BODY", "200000"))

PAUSE_CMD = os.environ.get(
    "AM_SAY_PAUSE_CMD", "/system/bin/cmd media_session dispatch pause")
RESUME_CMD = os.environ.get(
    "AM_SAY_RESUME_CMD", "/system/bin/cmd media_session dispatch play")

# One utterance at a time, matching what the ssh path gave for free by virtue
# of the caller serialising. Without it two requests would load clips into the
# same broker and cut each other off.
_speaking = threading.Lock()


def tailnet_ip() -> str:
    """The tailnet address, so this is not exposed to the cellular interface.

    Falls back to all-interfaces only when a token is set — an open speak-here
    endpoint on a phone's public IP is not something to offer by accident.
    """
    for cmd in (["/data/data/com.termux/files/usr/bin/tailscale", "ip", "-4"],
                ["tailscale", "ip", "-4"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=10).stdout.strip().splitlines()
            if out and out[0].strip():
                return out[0].strip()
        except (OSError, subprocess.SubprocessError):
            continue
    for name in ("tun0",):
        try:
            out = subprocess.run(["ip", "-4", "addr", "show", name],
                                 capture_output=True, text=True, timeout=5).stdout
            for tok in out.split():
                if tok.count(".") == 3 and tok.startswith("100."):
                    return tok.split("/")[0]
        except (OSError, subprocess.SubprocessError):
            pass
    return "" if not TOKEN else "0.0.0.0"


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "am-say-http"

    def log_message(self, fmt, *args):          # quiet by default
        if os.environ.get("AM_SAY_HTTP_DEBUG"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _authed(self) -> bool:
        if not TOKEN:
            return True
        return self.headers.get("X-Am-Token", "") == TOKEN

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return b""
        return self.rfile.read(n) if n else b""

    def _plain(self, code: int, text: str) -> None:
        payload = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):                            # noqa: N802
        if self.path.rstrip("/") in ("/health", ""):
            return self._plain(200, "ok\n")
        return self._plain(404, "not found\n")

    def do_POST(self):                           # noqa: N802
        if not self._authed():
            return self._plain(403, "forbidden\n")
        path = self.path.split("?", 1)[0].rstrip("/")

        if path in ("/pause", "/resume"):
            cmd = PAUSE_CMD if path == "/pause" else RESUME_CMD
            try:
                subprocess.run(cmd, shell=True, timeout=20,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
                return self._plain(200, "ok\n")
            except (OSError, subprocess.SubprocessError) as e:
                return self._plain(500, f"{e}\n")

        if path != "/say":
            return self._plain(404, "not found\n")

        text = self._body()
        if not text.strip():
            return self._plain(400, "empty body\n")
        if not os.path.exists(SAY_SH):
            return self._plain(500, f"missing {SAY_SH}\n")

        # Chunked, so CLIP and DURATION reach the caller when they happen
        # rather than after the audio finishes — the progress bar depends on
        # arriving early, which is the whole reason those lines exist.
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(data: bytes) -> None:
            self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        rc = 1
        with _speaking:
            try:
                proc = subprocess.Popen(
                    ["sh", SAY_SH], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                proc.stdin.write(text)
                proc.stdin.close()
                for line in proc.stdout:
                    chunk(line)
                rc = proc.wait()
            except (OSError, subprocess.SubprocessError) as e:
                try:
                    chunk(f"ERROR {e}\n".encode())
                except OSError:
                    pass
            finally:
                # A non-zero renderer exit has to reach the caller, and the
                # status line is long gone by now — so say it in the body and
                # let the trailing chunk carry it. remote-say greps stdout for
                # its protocol lines and checks the exit status of curl, so an
                # explicit marker is what survives.
                try:
                    if rc:
                        chunk(f"ERROR say.sh exited {rc}\n".encode())
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:
                    pass


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    host = tailnet_ip()
    if not host:
        sys.stderr.write(
            "am-say-http: no tailnet address found, and no AM_SAY_HTTP_TOKEN "
            "set — refusing to bind every interface on a phone\n")
        return 2
    if not shutil.which("sh"):
        return 2
    srv = Server((host, PORT), Handler)
    sys.stderr.write(f"am-say-http: listening on {host}:{PORT}, say.sh={SAY_SH}\n")
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
