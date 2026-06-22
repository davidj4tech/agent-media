from __future__ import annotations

import os

import httpx


def queue(track: dict, base_url: str | None = None) -> None:
    base = base_url or os.environ.get("ASTROTUNES_MEDIA_MCP", "http://127.0.0.1:8765")
    url = track.get("url")
    if not url:
        raise ValueError("media-mcp backend requires track['url']")
    r = httpx.post(
        f"{base}/api/cmd",
        json={"channel": "music", "name": "play", "args": {"url": url}},
        timeout=5,
    )
    r.raise_for_status()
