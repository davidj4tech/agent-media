"""sink-music-router: dispatch music-channel control to the live backend.

The music channel now has two playout backends:

  - `SinkMusic`        — Mopidy/MPD (whole-house via Snapcast, or local mel out)
  - `SinkMusicLocal`   — the phone's local mpv (residential download, offline)

The speech coordinator holds a single `self.music` and, before each clip, calls
`now_playing_uri()` then `duck()`/`pause()` on it. If music is on the phone but
`self.music` is Mopidy, `now_playing_uri()` returns None and the coordinator
bails — which is exactly why phone-local playout never ducked.

This router *is* a `Sink`: it resolves which backend is currently playing and
forwards each call there, so the coordinator ducks whatever is actually audible
without any change to the coordinator itself. When the phone backend isn't
configured (`MEDIA_MUSIC_LOCAL_ENDPOINT` unset) it degenerates to plain Mopidy.

Resolution rule: prefer the phone backend whenever it has a file loaded (playing
or paused — so a momentarily-paused phone track still ducks/restores correctly);
otherwise Mopidy. Each call re-resolves, which is cheap (one bridge probe) and
keeps a single source of truth even as playback moves between backends.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..types import Target
from .music import SinkMusic
from .music_local import SinkMusicLocal, configured as _local_configured


log = logging.getLogger(__name__)

# Target names that mean "play on the phone-local backend". Everything else
# (local, rooms, snapcast-*) is Mopidy's job.
_PHONE_TARGETS = {"phone", "local-phone", "phone-local"}


class SinkMusicRouter:
    """A `Sink` that forwards to the live music backend (Mopidy or phone-local)."""

    def __init__(self, mopidy: Optional[SinkMusic] = None,
                 local: Optional[SinkMusicLocal] = None) -> None:
        self.mopidy = mopidy or SinkMusic()
        self.local = local or SinkMusicLocal()

    # ---- backend resolution ---------------------------------------------

    def _local_live(self) -> bool:
        """True when the phone backend is configured AND has a track loaded."""
        if not _local_configured():
            return False
        try:
            return self.local.loaded()
        except Exception:  # noqa: BLE001 — bridge down ⇒ treat as not live
            return False

    def _observe_backend(self):
        """Backend the coordinator should observe/duck: phone if live, else Mopidy."""
        return self.local if self._local_live() else self.mopidy

    # ---- play routes by target ------------------------------------------

    def play(self, uri: str, target: Target = Target(name="local"),
             replace: bool = True, **opts) -> None:
        if target.name in _PHONE_TARGETS:
            self.local.play(uri, target, replace=replace, **opts)
        else:
            self.mopidy.play(uri, target, replace=replace, **opts)

    # ---- coordinator-facing control: follows the live backend -----------

    def now_playing_uri(self, target: Target = Target(name="local")) -> Optional[str]:
        return self._observe_backend().now_playing_uri(target)

    def position(self, target: Target = Target(name="local")) -> Optional[int]:
        return self._observe_backend().position(target)

    def pause(self, target: Target = Target(name="local")) -> None:
        self._observe_backend().pause(target)

    def resume(self, target: Target = Target(name="local")) -> None:
        self._observe_backend().resume(target)

    def stop(self, target: Target = Target(name="local")) -> None:
        self._observe_backend().stop(target)

    def duck(self, target: Target = Target(name="local"), level: int = 15) -> None:
        self._observe_backend().duck(target, level)

    def unduck(self, target: Target = Target(name="local"), restore: int = 100) -> None:
        self._observe_backend().unduck(target, restore)

    def seek_cur(self, target: Target = Target(name="local"),
                 position_ms: int = 0) -> None:
        self._observe_backend().seek_cur(target, position_ms)
