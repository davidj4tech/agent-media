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
import socket
from pathlib import Path
from typing import Any, Iterator, Optional


class MpvIpcError(RuntimeError):
    pass


_TCP_PREFIX = "tcp://"


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


def _send(sock_path: str | Path, command: list[Any], timeout: float = 5.0) -> dict:
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


def command(sock_path: str | Path, *args: Any, timeout: float = 5.0) -> Any:
    """Send `command` with positional args. Returns `data` from the reply,
    or raises MpvIpcError on non-success.
    """
    reply = _send(sock_path, list(args), timeout=timeout)
    if reply.get("error", "success") != "success":
        raise MpvIpcError(f"{args[0]}: {reply.get('error')}")
    return reply.get("data")


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


def get_property(sock_path: str | Path, name: str, timeout: float = 2.0) -> Any:
    return command(sock_path, "get_property", name, timeout=timeout)


def set_property(sock_path: str | Path, name: str, value: Any) -> None:
    command(sock_path, "set_property", name, value)
