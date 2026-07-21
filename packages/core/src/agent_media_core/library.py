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


def _suffix(target=None) -> str:
    """Env-var suffix for a target (accepts a Target, a name str, or None)."""
    name = getattr(target, "name", target) or ""
    return str(name).upper().replace("-", "_")


def _tenv(base: str, target=None) -> Optional[str]:
    """Per-target override of an env var, falling back to the global.

    e.g. MEDIA_AUDIOBOOK_ABS_DIR_ALICE -> MEDIA_AUDIOBOOK_ABS_DIR. Mirrors the
    existing MEDIA_SPEECH_PLAYOUT_MS_<TARGET> convention in submit.py.
    """
    if target is not None:
        sfx = _suffix(target)
        if sfx:
            v = os.environ.get(f"{base}_{sfx}")
            if v:
                return v
    return os.environ.get(base)


def library_dir(target=None) -> Path:
    """Where synced audiobook files live (override: MEDIA_AUDIOBOOK_LIB).

    Per-target override: MEDIA_AUDIOBOOK_LIB_<TARGET>.
    """
    override = _tenv("MEDIA_AUDIOBOOK_LIB", target)
    if override:
        return Path(override).expanduser()
    return Path.home() / "media" / "audiobooks"


def abs_import_dir(target=None) -> Path:
    """Host directory Audiobookshelf scans for books.

    Override with MEDIA_AUDIOBOOK_ABS_DIR / ABS_AUDIOBOOK_DIR (or their
    per-target _<TARGET> forms). The current container setup mounts
    ~/audiobooks as /audiobooks, so prefer that when it exists; fall back to
    the historical agent-media library.
    """
    override = _tenv("MEDIA_AUDIOBOOK_ABS_DIR", target) or _tenv("ABS_AUDIOBOOK_DIR", target)
    if override:
        return Path(override).expanduser()
    p = Path.home() / "audiobooks"
    return p if p.exists() else library_dir(target)


def _abs_cfg(target=None) -> tuple[str, str, str]:
    url = _tenv("MEDIA_AUDIOBOOKSHELF_URL", target) or _tenv("ABS_URL", target) or ""
    token = _tenv("MEDIA_AUDIOBOOKSHELF_TOKEN", target) or _tenv("ABS_TOKEN", target) or ""
    lib = _tenv("ABS_LIBRARY", target) or ""
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


def trigger_abs_scan(target=None) -> bool:
    """Ask Audiobookshelf to rescan its book library after an import.

    Per-target library selection via ABS_LIBRARY_<TARGET> (falls back to
    ABS_LIBRARY, then the first book-type library).
    """
    url, token, want = _abs_cfg(target)
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


def cached_path(vid: str, target=None) -> Optional[Path]:
    """The library file for video id `vid` (yt-dlp names it ``... [<vid>].ext``).

    Searches the per-target library dir when `target` is given.
    """
    d = library_dir(target)
    if not d.is_dir():
        return None
    # The bracketed id is unambiguous; match it literally to avoid title clashes.
    hits = sorted(d.glob(f"*[[]{vid}[]]*"))
    return hits[0] if hits else None


def fetch_cmd() -> Optional[str]:
    """Path to the `audiobook-fetch` acquisition helper, if installed."""
    return os.environ.get("MEDIA_AUDIOBOOK_FETCH") or shutil.which("audiobook-fetch")


def start_fetch(url: str, *, play: bool = False, target=None) -> bool:
    """Kick off a detached `audiobook-fetch` for `url`. Returns False if the
    helper isn't installed. With `play=True` the helper plays the result on the
    book channel when the (phone) download + sync finishes.

    When `target` is given, the helper syncs into that target's ABS library
    dir via the AUDIOBOOK_LIB env var it already honors. NOTE: the helper's
    internal `media book play` / `media abs-scan` calls don't forward a target,
    so playback/scan still hit the default book channel + library unless the
    external helper is updated to pass `--target` through."""
    fetch = fetch_cmd()
    if not fetch:
        return False
    argv = [fetch]
    if play:
        argv.append("--play")
    argv.append(url)
    env = None
    if target is not None and _suffix(target):
        env = dict(os.environ)
        env["AUDIOBOOK_LIB"] = str(abs_import_dir(target))
    subprocess.Popen(
        argv, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env=env,
    )
    return True
