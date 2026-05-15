"""Minimal mpv JSON-IPC client over a Unix socket.

Synchronous, one-shot per call. mpv replies with one JSON line per
command on the same socket; we read until newline or timeout.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any


class MpvIpcError(RuntimeError):
    pass


def _send(sock_path: str | Path, command: list[Any], timeout: float = 5.0) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock_path))
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


def get_property(sock_path: str | Path, name: str, timeout: float = 2.0) -> Any:
    return command(sock_path, "get_property", name, timeout=timeout)


def set_property(sock_path: str | Path, name: str, value: Any) -> None:
    command(sock_path, "set_property", name, value)
