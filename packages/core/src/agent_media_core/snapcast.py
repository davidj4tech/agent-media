"""Snapcast control: a thin JSON-RPC client for the snapserver control port.

The music channel routes audio to *rooms* via Snapcast: a snapserver carries
named streams (``am``, ``am-music``, …), each fed by a source (a pipe fed by a
player), and snapclients in each room subscribe to a group → stream. Two things
agent-media needs from Snapcast:

  - **routing** — which stream a group of rooms plays (``Group.SetStream``), and
    who is connected where (``Server.GetStatus``); this powers the auto
    rooms-vs-phone decision and, later, switching rooms onto a residential
    source.
  - **ducking** — lower the rooms' music volume under speech via
    ``Group.SetVolume`` / ``Client.SetVolume`` instead of (or alongside) Mopidy's
    own ``setvol``, so the duck works regardless of which player feeds the stream.

Config: ``MEDIA_SNAP_JSONRPC_HOST`` / ``MEDIA_SNAP_JSONRPC_PORT`` (default the
local snapserver at 127.0.0.1:1705 — set the host to the rooms hub, e.g. red5).
All methods are best-effort and connection-scoped (one TCP round-trip each); the
caller decides how to handle ``SnapcastError``.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from typing import Any, Optional


log = logging.getLogger(__name__)


class SnapcastError(RuntimeError):
    pass


def endpoint() -> tuple[str, int]:
    host = os.environ.get("MEDIA_SNAP_JSONRPC_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("MEDIA_SNAP_JSONRPC_PORT", "1705"))
    except ValueError:
        port = 1705
    return host, port


def phone_client_prefix() -> str:
    """Snapclient id prefix that marks the phone (excluded from 'other rooms')."""
    return os.environ.get("MEDIA_SNAP_PHONE_PREFIX",
                          os.environ.get("MUSIC_PHONE_CLIENT_PREFIX", "p8ar"))


def _rpc(method: str, params: Optional[dict] = None,
         timeout: float = 4.0) -> Any:
    """One JSON-RPC call to the snapserver control port. Returns ``result``."""
    host, port = endpoint()
    req = {"id": 1, "jsonrpc": "2.0", "method": method}
    if params:
        req["params"] = params
    try:
        s = socket.create_connection((host, port), timeout)
    except OSError as e:
        raise SnapcastError(f"connect {host}:{port}: {e}")
    try:
        s.settimeout(timeout)
        s.sendall((json.dumps(req) + "\r\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
        line = buf.split(b"\n", 1)[0]
        if not line:
            raise SnapcastError(f"{method}: empty reply")
        msg = json.loads(line.decode())
        if "error" in msg:
            raise SnapcastError(f"{method}: {msg['error']}")
        return msg.get("result")
    except (OSError, ValueError) as e:
        raise SnapcastError(f"{method}: {e}")
    finally:
        s.close()


def get_status(timeout: float = 4.0) -> dict:
    """Full ``Server.GetStatus`` server object (groups, clients, streams)."""
    result = _rpc("Server.GetStatus", timeout=timeout)
    return (result or {}).get("server", {})


def connected_other_clients(timeout: float = 4.0) -> list[str]:
    """Connected snapclient ids that are NOT the phone (the 'other rooms').

    Used by the auto rooms-vs-phone route: if any non-phone room is listening,
    a play is whole-house intent → rooms; otherwise only the phone is up →
    phone-local. Raises SnapcastError if the server is unreachable so the caller
    can fall back to a configured default rather than guessing.
    """
    prefix = phone_client_prefix()
    server = get_status(timeout=timeout)
    out: list[str] = []
    for g in server.get("groups", []):
        for c in g.get("clients", []):
            if c.get("connected") and not str(c.get("id", "")).startswith(prefix):
                out.append(c["id"])
    return out


def groups_on_stream(stream_id: str, timeout: float = 4.0) -> list[str]:
    """Ids of groups currently playing `stream_id`."""
    server = get_status(timeout=timeout)
    return [g["id"] for g in server.get("groups", [])
            if g.get("stream_id") == stream_id]


def clients_on_stream(stream_id: str, timeout: float = 4.0,
                      audible_only: bool = True) -> list[dict]:
    """Clients subscribed (via their group) to ``stream_id``.

    Returns ``{id, percent, muted, connected}`` per client. With
    ``audible_only`` (default) only connected, un-muted clients are returned —
    the rooms actually *playing* the stream — so a volume duck touches just the
    audible members and leaves muted/offline rooms to the priority watcher.
    ``Group.SetVolume`` isn't in the snapserver RPC, so ducking is done
    per-client via :func:`set_client_volume` against these ids.
    """
    server = get_status(timeout=timeout)
    out: list[dict] = []
    for g in server.get("groups", []):
        if g.get("stream_id") != stream_id:
            continue
        for c in g.get("clients", []):
            vol = (c.get("config") or {}).get("volume") or {}
            connected = bool(c.get("connected"))
            muted = bool(vol.get("muted"))
            if audible_only and (not connected or muted):
                continue
            out.append({"id": c.get("id"),
                        "percent": int(vol.get("percent", 100)),
                        "muted": muted,
                        "connected": connected})
    return out


def set_group_stream(group_id: str, stream_id: str,
                     timeout: float = 4.0) -> None:
    """Point a group at a stream (``Group.SetStream``)."""
    _rpc("Group.SetStream",
         {"id": group_id, "stream_id": stream_id}, timeout=timeout)


def set_group_volume(group_id: str, percent: int,
                     muted: Optional[bool] = None, timeout: float = 4.0) -> None:
    """Set a group's volume 0-100 (``Group.SetVolume``)."""
    vol: dict = {"percent": max(0, min(100, percent))}
    if muted is not None:
        vol["muted"] = bool(muted)
    _rpc("Group.SetVolume", {"id": group_id, "volume": vol}, timeout=timeout)


def set_client_volume(client_id: str, percent: int,
                      muted: Optional[bool] = None, timeout: float = 4.0) -> None:
    """Set a client's volume 0-100 (``Client.SetVolume``)."""
    vol: dict = {"percent": max(0, min(100, percent))}
    if muted is not None:
        vol["muted"] = bool(muted)
    _rpc("Client.SetVolume", {"id": client_id, "volume": vol}, timeout=timeout)
