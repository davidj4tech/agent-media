"""Audiobook library: resolve YouTube URIs to locally-cached files.

mel's IP is an IONOS datacenter address that YouTube blocks (SABR/PO-token),
so mel can neither stream nor download YouTube directly. The book channel's
mpv therefore cannot play `yt:`/youtube URLs at all. Instead we acquire the
audio on the phone (residential IP) via `audiobook-fetch`, sync it into a
local library, and the book channel plays the *local file*.

This module is the glue: detect a YouTube URI, map it to its cached file (by
the video id yt-dlp embeds in the filename, ``... [<id>].<ext>``), and — on a
miss — kick off `audiobook-fetch` to acquire it.

See memory: project_youtube_acquisition_phone.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional


# Bare 11-char YouTube id from the common URL shapes (after any `yt:` strip).
_YT_ID = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/)|youtu\.be/)"
    r"([0-9A-Za-z_-]{11})"
)
_YT_HOST = re.compile(r"^https?://(?:[\w-]+\.)?(?:youtube\.com|youtu\.be)/", re.I)


def library_dir() -> Path:
    """Where synced audiobook files live (override: MEDIA_AUDIOBOOK_LIB)."""
    override = os.environ.get("MEDIA_AUDIOBOOK_LIB")
    if override:
        return Path(override).expanduser()
    return Path.home() / "media" / "audiobooks"


def is_youtube(uri: str) -> bool:
    return bool(_YT_HOST.match(uri.strip()))


def video_id(uri: str) -> Optional[str]:
    m = _YT_ID.search(uri)
    return m.group(1) if m else None


def cached_path(vid: str) -> Optional[Path]:
    """The library file for video id `vid` (yt-dlp names it ``... [<vid>].ext``)."""
    d = library_dir()
    if not d.is_dir():
        return None
    # The bracketed id is unambiguous; match it literally to avoid title clashes.
    hits = sorted(d.glob(f"*[[]{vid}[]]*"))
    return hits[0] if hits else None


def fetch_cmd() -> Optional[str]:
    """Path to the `audiobook-fetch` acquisition helper, if installed."""
    return os.environ.get("MEDIA_AUDIOBOOK_FETCH") or shutil.which("audiobook-fetch")


def start_fetch(url: str, *, play: bool = False) -> bool:
    """Kick off a detached `audiobook-fetch` for `url`. Returns False if the
    helper isn't installed. With `play=True` the helper plays the result on the
    book channel when the (phone) download + sync finishes."""
    fetch = fetch_cmd()
    if not fetch:
        return False
    argv = [fetch]
    if play:
        argv.append("--play")
    argv.append(url)
    subprocess.Popen(
        argv, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return True
