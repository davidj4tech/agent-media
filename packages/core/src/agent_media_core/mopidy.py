"""Minimal Mopidy JSON-RPC client.

Used by the book/music sinks (under MEDIA_BOOK_MOPIDY / MEDIA_MUSIC_MOPIDY)
to route playback through Mopidy so Iris's history view sees every play,
instead of poking the mpv socket directly behind Mopidy's back.

The endpoints are env-configurable:

- MEDIA_MOPIDY_BOOK_URL   (default http://127.0.0.1:6681)
- MEDIA_MOPIDY_MUSIC_URL  (default http://127.0.0.1:6680)

Callers are expected to catch MopidyRpcError and fall back to direct ipc.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 4.0


class MopidyRpcError(RuntimeError):
    """Raised when a Mopidy RPC call fails (network, HTTP, or JSON-RPC error)."""


def book_url() -> str:
    return os.environ.get("MEDIA_MOPIDY_BOOK_URL", "http://127.0.0.1:6681")


def music_url() -> str:
    return os.environ.get("MEDIA_MOPIDY_MUSIC_URL", "http://127.0.0.1:6680")


def rpc(base_url: str, method: str, *, timeout: float = _DEFAULT_TIMEOUT_S,
        **params: Any) -> Any:
    """POST a JSON-RPC call to {base_url}/mopidy/rpc. Returns the result field.

    Raises MopidyRpcError on transport failure, non-2xx, or jsonrpc `error`.
    """
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params:
        body["params"] = params
    data = json.dumps(body).encode("utf-8")
    url = base_url.rstrip("/") + "/mopidy/rpc"
    req = urllib.request.Request(
        url, data=data,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        raise MopidyRpcError(f"{method}: {e}") from e
    if "error" in payload:
        raise MopidyRpcError(f"{method}: {payload['error']}")
    return payload.get("result")


def play_uri(base_url: str, uri: str, *, replace: bool = True,
             timeout: float = _DEFAULT_TIMEOUT_S) -> None:
    """Clear, add, and play `uri` on the Mopidy instance at `base_url`.

    With replace=False, appends to the existing tracklist instead.
    """
    if replace:
        rpc(base_url, "core.tracklist.clear", timeout=timeout)
    rpc(base_url, "core.tracklist.add", uris=[uri], timeout=timeout)
    rpc(base_url, "core.playback.play", timeout=timeout)
