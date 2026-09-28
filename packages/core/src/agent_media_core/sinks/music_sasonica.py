"""sink-music-sasonica: music played by Sasonica's own media player.

The `sasonica` music target. Sasonica (``com.sasonica.app``) runs a second
Media3 player beside its speech player, on its own port (6615), answering
mpv's JSON IPC the way the speech one does (sasonica-app ``Media3Music``). So
this is the phone-mpv backend (``SinkMusicLocal``) with a different endpoint
and one different verb:

  - **play** cannot hand the app a path in Termux's cache, or run yt-dlp on
    it. A YouTube URI is fetched on the phone (play-local ``--fetch-only``,
    skipped when the phone already has it) and handed over as a loopback URL
    on the phone's own music-files service (deploy/phone/service/music-files,
    127.0.0.1:6616). The ``abs`` route (copy it to red5, serve it back) is
    the fallback: over the phone's link it moved ~8 KB/s on 28 Sep 2026, so
    a 52 MB mix would have taken an hour to start.
  - **everything else** (pause, seek, speed, volume, next, the observation
    reads the coordinator follows) is the mpv IPC it inherits.

The app takes ordinary media audio focus, and its speech takes transient
focus, so a reply pauses the music inside the phone with no round trip here.
The coordinator's own duck still reaches it as a volume write, on the app's
0-100 scale rather than the Termux mpv's 0-170.

Why a second app player rather than Sasonica ABS's: docs/proposals/
2026-09-28-music-tab.md (David, 28 Sep 2026).

Config: ``MEDIA_MUSIC_SASONICA_ENDPOINT``, e.g. ``tcp://p8a:6615``. Unset,
the target is not offered. ``MEDIA_MUSIC_SASONICA_FILES`` is the music-files
base the app reads from (default ``http://localhost:6616``: the app permits
cleartext to ``localhost`` by name, not to 127.0.0.1), or ``off`` to
use the red5 route only.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import urllib.parse
from typing import Optional

from ..types import Target
from . import _mpv_ipc as ipc
from . import music_app, music_fetch, music_local
from .music_local import SinkMusicLocal, _note_title

log = logging.getLogger(__name__)

SASONICA_TARGET = Target(name="sasonica")


def endpoint() -> Optional[str]:
    ep = os.environ.get("MEDIA_MUSIC_SASONICA_ENDPOINT", "").strip()
    return ep or None


def configured() -> bool:
    return endpoint() is not None


def files_base() -> Optional[str]:
    base = os.environ.get("MEDIA_MUSIC_SASONICA_FILES", "http://localhost:6616").strip()
    return None if base.lower() in ("", "off", "0", "no") else base.rstrip("/")


def _phone_fetch(uri: str) -> Optional[str]:
    """Download `uri` into the phone's cache (play-local --fetch-only); its path."""
    timeout = float(os.environ.get("MEDIA_MUSIC_LOCAL_FETCH_TIMEOUT", "600"))
    remote = f"{music_local.fetch_cmd()} --fetch-only {shlex.quote(uri)}"
    try:
        r = subprocess.run(music_local.phone_argv(remote),
                           capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("sink-music-sasonica: phone fetch failed: %s", e)
        return None
    lines = [ln for ln in (r.stdout or "").strip().splitlines() if ln]
    if r.returncode != 0 or not lines or not lines[-1].startswith("/"):
        log.warning("sink-music-sasonica: phone fetch failed (%d): %s", r.returncode,
                    (r.stderr or r.stdout or "").strip()[-300:])
        return None
    return lines[-1]


def phone_resolve(uri: str) -> tuple[Optional[str], str]:
    """``(url, title)`` on the phone's music-files service; url None = not there."""
    base = files_base()
    raw = uri[3:] if uri.startswith("yt:") else uri
    vid = music_fetch.watch_id(raw)
    if not base or not vid:
        return None, ""
    path = music_local._phone_cached_path(vid) or _phone_fetch(raw)
    if not path:
        return None, ""
    name = path.rsplit("/", 1)[-1]
    return f"{base}/{urllib.parse.quote(name)}", music_local._phone_title(vid)


def resolve(uri: str) -> tuple[Optional[str], str]:
    """The phone's own copy first, then the red5 route ``abs`` uses."""
    url, title = phone_resolve(uri)
    return (url, title) if url else music_app.resolve(uri)


class SinkMusicSasonica(SinkMusicLocal):
    """The music channel's Sasonica backend (the app's media player)."""

    def _endpoint(self) -> str:
        ep = self._ep_override or endpoint()
        if not ep:
            raise ipc.MpvIpcError("sink-music-sasonica: MEDIA_MUSIC_SASONICA_ENDPOINT unset")
        return ep

    def play(self, uri: str, target: Target = SASONICA_TARGET,
             replace: bool = True, **_: object) -> bool:
        """Load `uri` in the app. False = it could not be; the caller falls back."""
        if not (self._ep_override or configured()):
            return False
        url, title = resolve(uri)
        if not url:
            log.info("sink-music-sasonica: nothing the app can play for %s", uri)
            return False
        try:
            ipc.command(self._endpoint(), "loadfile", url,
                        "replace" if replace else "append-play")
            if title and replace:
                self._set("force-media-title", title)
        except (ipc.MpvIpcError, OSError) as e:
            log.info("sink-music-sasonica: the app did not take %s: %s", uri, e)
            return False
        self._unpause_if(replace)
        if title:
            _note_title(uri, title)
        return True

    def seek_cur(self, target: Target = SASONICA_TARGET, position_ms: int = 0) -> None:
        # The app's socket takes `seek`, not a write to `time-pos` (the
        # speech channel never needed one), so a jump is the command.
        try:
            ipc.command(self._endpoint(), "seek", max(0.0, position_ms / 1000.0), "absolute")
        except (ipc.MpvIpcError, OSError):
            pass

    # The app's volume is ExoPlayer's 0-100, not the Termux mpv's 0-170.
    def nominal_volume(self, target: Target = SASONICA_TARGET) -> int:
        return 100

    def unduck(self, target: Target = SASONICA_TARGET, restore: int = 100) -> None:
        try:
            self._set("volume", max(0, min(100, restore)))
        except (ipc.MpvIpcError, OSError):
            pass

    def volume_delta(self, delta: int, target: Target = SASONICA_TARGET) -> None:
        try:
            cur = ipc.get_property(self._endpoint(), "volume")
            self._set("volume", max(0, min(100, int(round((cur or 100) + delta)))))
        except (ipc.MpvIpcError, OSError, TypeError, ValueError):
            pass
