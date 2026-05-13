from __future__ import annotations

import os
import subprocess


def queue(track: dict, host: str | None = None) -> None:
    """Add track to Mopidy via `mpc`. Expects a YouTube URL in track['url']."""
    host = host or os.environ.get("ASTROTUNES_MPD_HOST", "p8ar")
    url = track.get("url")
    if not url:
        raise ValueError("mpd backend requires track['url']")
    yt = url if url.startswith("yt:") else f"yt:{url}"
    subprocess.run(["mpc", "-h", host, "add", yt], check=True)
