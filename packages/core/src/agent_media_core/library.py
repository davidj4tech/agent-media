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
import urllib.request


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


def abs_import_dir() -> Path:
    """Host directory Audiobookshelf scans for books.

    Override with MEDIA_AUDIOBOOK_ABS_DIR / ABS_AUDIOBOOK_DIR. The current
    container setup mounts ~/audiobooks as /audiobooks, so prefer that when it
    exists; fall back to the historical agent-media library.
    """
    override = os.environ.get("MEDIA_AUDIOBOOK_ABS_DIR") or os.environ.get("ABS_AUDIOBOOK_DIR")
    if override:
        return Path(override).expanduser()
    p = Path.home() / "audiobooks"
    return p if p.exists() else library_dir()


def _abs_cfg() -> tuple[str, str, str]:
    url = os.environ.get("MEDIA_AUDIOBOOKSHELF_URL") or os.environ.get("ABS_URL") or ""
    token = os.environ.get("MEDIA_AUDIOBOOKSHELF_TOKEN") or os.environ.get("ABS_TOKEN") or ""
    lib = os.environ.get("ABS_LIBRARY", "")
    try:
        for line in (Path.home() / ".config" / "agent-media" / "abs-bridge.env").read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"\'')
            if k == "ABS_URL" and not url:
                url = v
            elif k == "ABS_TOKEN" and not token:
                token = v
            elif k == "ABS_LIBRARY" and not lib:
                lib = v
    except OSError:
        pass
    return url.rstrip("/"), token, lib


def trigger_abs_scan() -> bool:
    """Ask Audiobookshelf to rescan its book library after an import."""
    url, token, want = _abs_cfg()
    if not url or not token:
        return False
    try:
        req = urllib.request.Request(f"{url}/api/libraries", headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            libs = __import__("json").loads(r.read()).get("libraries", [])
        lib = next((l for l in libs if l.get("id") == want or l.get("name") == want), None) if want else None
        lib = lib or next((l for l in libs if l.get("mediaType") == "book"), None)
        if not lib:
            return False
        req = urllib.request.Request(f"{url}/api/libraries/{lib['id']}/scan", method="POST",
                                     headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception:
        return False


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
