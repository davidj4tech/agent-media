"""sink-music-sasonica: music played by Sasonica's own media player.

The `sasonica` music target. Sasonica (``com.sasonica.app``) runs a second
Media3 player beside its speech player, on its own port (6615), answering
mpv's JSON IPC the way the speech one does (sasonica-app ``Media3Music``). So
this is the phone-mpv backend (``SinkMusicLocal``) with a different endpoint
and one different verb:

  - **play** cannot hand the app a path in Termux's cache, or run yt-dlp on
    it. A YouTube URI is fetched as the ``abs`` target fetches it
    (``music_app.resolve``: on the phone's residential IP, copied into red5's
    cache, served from red5's clip server) and loaded as that URL.
  - **everything else** (pause, seek, speed, volume, next, the observation
    reads the coordinator follows) is the mpv IPC it inherits.

The app takes ordinary media audio focus, and its speech takes transient
focus, so a reply pauses the music inside the phone with no round trip here.
The coordinator's own duck still reaches it as a volume write, on the app's
0-100 scale rather than the Termux mpv's 0-170.

Why a second app player rather than Sasonica ABS's: docs/proposals/
2026-09-28-music-tab.md (David, 28 Sep 2026).

Config: ``MEDIA_MUSIC_SASONICA_ENDPOINT``, e.g. ``tcp://p8a:6615``. Unset,
the target is not offered.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from ..types import Target
from . import _mpv_ipc as ipc
from . import music_app
from .music_local import SinkMusicLocal, _note_title

log = logging.getLogger(__name__)

SASONICA_TARGET = Target(name="sasonica")


def endpoint() -> Optional[str]:
    ep = os.environ.get("MEDIA_MUSIC_SASONICA_ENDPOINT", "").strip()
    return ep or None


def configured() -> bool:
    return endpoint() is not None


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
        url, title = music_app.resolve(uri)
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
