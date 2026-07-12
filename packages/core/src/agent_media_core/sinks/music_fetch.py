"""Rooms-side YouTube acquisition: download on the phone, play a local file.

YouTube fully bot-blocks red5's datacenter IP (as of 2026-07 every yt-dlp
player client gets "Sign in to confirm you're not a bot"), so the rooms path
can no longer stream watch URLs at all — not even through the Mopidy-Mpv
backend's yt-dlp. The reliable route is the one the phone backend already
uses: fetch the audio on the phone's residential IP (`play-local
--fetch-only`, cached by video id, title+chapters embedded in .mka), copy the
file into the Mopidy host's cache, and play it as a local ``mpv:`` file.
Repeats hit the rooms cache and never touch the phone.

Everything degrades gracefully: when the phone fetcher is unreachable (or
disabled with MEDIA_MUSIC_ROOMS_FETCH=0) `ensure_local` returns None and the
caller falls back to queueing the plain watch URL — the old streaming path,
which starts working again the day the IP block lifts.

Config (all optional):

  MEDIA_MUSIC_LOCAL_SSH / MEDIA_MUSIC_LOCAL_FETCH
      the residential fetcher, shared with sink-music-local
      (default ``p8ar`` / ``bin/play-local``).
  MEDIA_MUSIC_ROOMS_SSH
      host that renders rooms audio (runs Mopidy + mopidy-mpv). Default:
      derived from MEDIA_MPD_HOST/MPD_HOST — loopback or this machine means
      local execution, anything else is reached over ssh. Satellites that
      point MPD at the hub therefore need no extra env.
  MEDIA_MUSIC_ROOMS_CACHE
      $HOME-relative cache dir on that host (default ``.cache/music-offline``,
      the same layout play-local uses on the phone).
  MEDIA_MUSIC_ROOMS_FETCH_TIMEOUT
      seconds for the phone-side download (default 600 — DJ sets are long).
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import socket
import subprocess
from typing import List, Optional

from . import music_local

log = logging.getLogger(__name__)

_WATCH_ID_RE = re.compile(
    r"(?:[?&]v=|youtu\.be/|/shorts/|/live/)([A-Za-z0-9_-]{11})")

# Reuse the phone's ControlMaster options so repeated fetches are cheap.
_SSH_OPTS = music_local._SSH_OPTS


def enabled() -> bool:
    return os.environ.get("MEDIA_MUSIC_ROOMS_FETCH", "1") != "0"


def rooms_ssh_host() -> Optional[str]:
    """Host to run rooms-side file commands on, or None for local execution."""
    override = os.environ.get("MEDIA_MUSIC_ROOMS_SSH")
    if override is not None:
        return override.strip() or None
    host = (os.environ.get("MEDIA_MPD_HOST")
            or os.environ.get("MPD_HOST", "127.0.0.1"))
    if host in ("127.0.0.1", "::1", "localhost"):
        return None
    if host.split(".")[0] == socket.gethostname().split(".")[0]:
        return None
    return host


def cache_dir() -> str:
    return os.environ.get("MEDIA_MUSIC_ROOMS_CACHE", ".cache/music-offline")


def watch_id(url: str) -> Optional[str]:
    """The 11-char video id of a YouTube URL/id, or None."""
    u = url.strip()
    m = _WATCH_ID_RE.search(u)
    if m:
        return m.group(1)
    return u if re.fullmatch(r"[A-Za-z0-9_-]{11}", u) else None


def _rooms_run(script: str, *, stdin=None, timeout: float = 30.0
               ) -> subprocess.CompletedProcess:
    """Run a shell snippet on the rooms host (locally when we are it)."""
    host = rooms_ssh_host()
    if host is None:
        argv = ["sh", "-c", script]
    else:
        argv = ["ssh", *_SSH_OPTS, host, script]
    return subprocess.run(argv, stdin=stdin, capture_output=True, text=True,
                          timeout=timeout)


def _cached_path(vid: str) -> Optional[str]:
    r = _rooms_run(
        f"ls -1 \"$HOME\"/{shlex.quote(cache_dir())}/{vid}.* 2>/dev/null"
        " | grep -v -e '\\.title$' -e '\\.part$' | head -1")
    path = (r.stdout or "").strip().splitlines()
    return path[0] if r.returncode == 0 and path else None


def _phone_fetch(url: str) -> Optional[str]:
    """Download `url` on the phone; return the phone-side file path."""
    timeout = float(os.environ.get("MEDIA_MUSIC_ROOMS_FETCH_TIMEOUT", "600"))
    remote = f"{music_local.fetch_cmd()} --fetch-only {shlex.quote(url)}"
    try:
        r = subprocess.run(["ssh", *_SSH_OPTS, music_local.ssh_host(), remote],
                           capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("music_fetch: phone fetch failed: %s", e)
        return None
    if r.returncode != 0:
        log.warning("music_fetch: phone fetch failed (%d): %s", r.returncode,
                    (r.stderr or r.stdout).strip()[-300:])
        return None
    lines = [ln for ln in (r.stdout or "").strip().splitlines() if ln]
    return lines[-1] if lines and lines[-1].startswith("/") else None


def _copy_to_rooms(phone_path: str) -> Optional[str]:
    """Stream a phone-side file (and its .title sidecar) into the rooms cache."""
    base = phone_path.rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0]
    cache = shlex.quote(cache_dir())
    qbase, qstem = shlex.quote(base), shlex.quote(stem)
    reader = subprocess.Popen(
        ["ssh", *_SSH_OPTS, music_local.ssh_host(), f"cat {shlex.quote(phone_path)}"],
        stdout=subprocess.PIPE)
    try:
        r = _rooms_run(
            f"mkdir -p \"$HOME\"/{cache} && "
            f"cat > \"$HOME\"/{cache}/.{qbase}.part && "
            f"mv \"$HOME\"/{cache}/.{qbase}.part \"$HOME\"/{cache}/{qbase} && "
            f"echo \"$HOME\"/{cache}/{qbase}",
            stdin=reader.stdout, timeout=600.0)
    finally:
        reader.stdout.close()
        reader.wait()
    if reader.returncode != 0 or r.returncode != 0:
        log.warning("music_fetch: copy to rooms failed: %s",
                    (r.stderr or "").strip()[-200:])
        return None
    dest = (r.stdout or "").strip().splitlines()
    # Best-effort title sidecar (for cache files predating embedded tags).
    sidecar = subprocess.Popen(
        ["ssh", *_SSH_OPTS, music_local.ssh_host(),
         f"cat {shlex.quote(phone_path.rsplit('.', 1)[0] + '.title')} 2>/dev/null"],
        stdout=subprocess.PIPE)
    try:
        _rooms_run(
            f"cat > \"$HOME\"/{cache}/{qstem}.title; "
            f"[ -s \"$HOME\"/{cache}/{qstem}.title ] || rm -f \"$HOME\"/{cache}/{qstem}.title",
            stdin=sidecar.stdout, timeout=30.0)
    except Exception:  # noqa: BLE001 — sidecar is cosmetic
        pass
    finally:
        sidecar.stdout.close()
        sidecar.wait()
    return dest[0] if dest else None


def ensure_local(url: str) -> Optional[str]:
    """Rooms-local file path for a YouTube watch URL, fetching if needed.

    Returns None when fetching is disabled or fails — callers fall back to
    the plain streaming URI.
    """
    if not enabled():
        return None
    vid = watch_id(url)
    if vid:
        cached = _cached_path(vid)
        if cached:
            return cached
    try:
        phone_path = _phone_fetch(url)
        if not phone_path:
            return None
        return _copy_to_rooms(phone_path)
    except Exception as e:  # noqa: BLE001 — never break play() on fetch bugs
        log.warning("music_fetch: %s", e)
        return None


def append_fetched(urls: List[str]) -> None:
    """Fetch each URL and append it to the rooms queue (detached helper).

    Used for playlist tails: the first track plays immediately, the rest
    arrive as their downloads finish. Tracks that fail to fetch are appended
    as plain watch URLs so the queue order is preserved.
    """
    from .music import SinkMusic
    m = SinkMusic()
    for url in urls:
        path = ensure_local(url)
        uri = f"mpv:{path}" if path else f"mpv:{url}"
        try:
            m.enqueue(uri)
        except OSError as e:
            log.warning("music_fetch: enqueue failed: %s", e)
            return


def spawn_append_fetched(urls: List[str]) -> None:
    """Run append_fetched in a detached process so play() returns promptly."""
    if not urls:
        return
    import sys
    subprocess.Popen(
        [sys.executable, "-m", "agent_media_core.sinks.music_fetch", *urls],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    append_fetched(sys.argv[1:])
