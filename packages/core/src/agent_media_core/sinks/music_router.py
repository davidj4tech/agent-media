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
import os
from typing import Optional

from ..types import Target
from .music import SinkMusic
from .music_local import SinkMusicLocal, configured as _local_configured


log = logging.getLogger(__name__)

# Target names that mean "play on the phone-local backend". Everything else
# (local, rooms, snapcast-*) is Mopidy's job.
_PHONE_TARGETS = {"phone", "local-phone", "phone-local"}


def default_target() -> Target:
    """Default music target: music override, then speech/default device."""
    return Target(os.environ.get("MEDIA_MUSIC_DEFAULT_TARGET")
                  or os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET")
                  or "local")


def _resolve_target(target: Target) -> Target:
    if target.name in ("", "local"):
        return default_target()
    return target


class SinkMusicRouter:
    """A `Sink` that forwards to the live music backend (Mopidy or phone-local)."""

    def __init__(self, mopidy: Optional[SinkMusic] = None,
                 local: Optional[SinkMusicLocal] = None) -> None:
        self.mopidy = mopidy or SinkMusic()
        self.local = local or SinkMusicLocal()
        # Which backend the in-force duck was sent to. See duck()/unduck().
        self._ducked_backend = None

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

    def _backend_for(self, target: Target):
        """Backend for a call that names a target explicitly.

        _observe_backend answers "what is audible right now", which is the right
        question for the coordinator's untargeted duck/restore. It is the wrong
        question when the CALLER already said `phone`: liveness is decided by a
        ~1.1s probe across the tailnet, so a single slow or breakered probe made
        a phone-targeted read fall through to Mopidy -- which does not implement
        `phone` and raises NotImplementedError rather than degrading. That made
        music_now_playing fail intermittently while the phone was audibly
        playing, since it re-resolves once per property it reads.

        Routing an explicit phone target straight to the phone backend mirrors
        what play() already does and makes these reads deterministic. Untargeted
        calls (target "local"/"" -> default) keep observing, unchanged.
        """
        # NOTE: the RAW target, deliberately not _resolve_target(). Resolving
        # first would turn an untargeted call into the default target -- which
        # is 'phone' on this host -- and so send every observe-style read to the
        # phone, defeating the fall-back-to-Mopidy-when-phone-idle behaviour.
        # Only a caller that NAMED a phone target gets deterministic routing.
        if target.name in _PHONE_TARGETS:
            return self.local
        return self._observe_backend()

    # ---- play routes by target ------------------------------------------

    def play(self, uri: str, target: Target = Target(name="local"),
             replace: bool = True, **opts) -> None:
        target = _resolve_target(target)
        if target.name in _PHONE_TARGETS:
            self.local.play(uri, target, replace=replace, **opts)
        else:
            self.mopidy.play(uri, target, replace=replace, **opts)

    # ---- coordinator-facing control: follows the live backend -----------

    def now_playing_uri(self, target: Target = Target(name="local")) -> Optional[str]:
        return self._backend_for(target).now_playing_uri(target)

    def position(self, target: Target = Target(name="local")) -> Optional[int]:
        return self._backend_for(target).position(target)

    def pause(self, target: Target = Target(name="local")) -> None:
        self._backend_for(target).pause(target)

    def resume(self, target: Target = Target(name="local")) -> None:
        self._backend_for(target).resume(target)

    def stop(self, target: Target = Target(name="local")) -> None:
        self._backend_for(target).stop(target)

    def duck(self, target: Target = Target(name="local"), level: int = 15) -> None:
        backend = self._observe_backend()
        self._ducked_backend = backend
        backend.duck(target, level)

    def unduck(self, target: Target = Target(name="local"), restore: int = 100) -> None:
        """Restore the volume of whoever was ducked — not of whoever is live now.

        _observe_backend answers "what is audible", and liveness includes *has a
        track loaded*. A reply that outlasts the track it was ducked under
        therefore flips the answer between the duck and the restore: on p8a on
        2026-08-15 the duck at 07:42:51 went to the phone, the track ended at
        07:45:01, and the restore at 07:45:31 was routed to Mopidy — which had
        nothing to restore. The phone sat at 10 with nothing left that knew, and
        the next thing to play there would have been near-silent.

        This is the fourth cause of the same sentence ("the music got quieter
        after Sam spoke and never came back") and the same shape as the other
        three: one owner per volume, and the owner is whoever took it down.
        """
        backend = self._ducked_backend or self._observe_backend()
        self._ducked_backend = None
        backend.unduck(target, restore)

    # seek_cur is deliberately NOT declared here: __getattr__ already routes
    # it to the live backend, and does so with the caller's arguments passed
    # through untouched. An explicit wrapper had to invent a 'target' the
    # caller never supplied, which broke any backend whose seek_cur takes
    # position_ms alone -- the CLI calls it exactly that way.

    # ---- everything else: follow the live backend, fall back to Mopidy ---

    def __getattr__(self, name):
        """Forward any un-routed attribute to the backend that is actually live.

        The explicit methods above cover the coordinator's duck/observe
        contract, but `SinkMusic` has a far wider surface (enqueue, next,
        toggle, set_speed, status_dict, current_song, volume_delta, ...). The
        CLI treats its music sink as that wider object, so without this a
        router substituted for a SinkMusic would AttributeError on any of them.

        Resolution mirrors _observe_backend: prefer whatever is audible, so a
        phone track answers while it is playing. SinkMusicLocal is deliberately
        a NARROWER Sink, though, so anything it does not implement falls back to
        Mopidy rather than raising -- a phone-live `next` still drives the
        Mopidy queue exactly as it did before.

        Returning the bound method (rather than wrapping the call) is what lets
        the caller's arguments through untouched -- see the seek_cur note above.

        Only called when normal lookup fails, so the routed methods above keep
        precedence. `mopidy`/`local`/underscore names are excluded so a lookup
        during __init__ cannot recurse through _observe_backend before those
        attributes are bound.
        """
        if name.startswith("_") or name in ("mopidy", "local"):
            raise AttributeError(name)
        backend = self._observe_backend()
        attr = getattr(backend, name, None)
        if attr is None and backend is not self.mopidy:
            attr = getattr(self.mopidy, name, None)
        if attr is None:
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute {name!r}")
        return attr

    def current_volume(self, target: Target = Target(name="local")):
        """The live backend's volume — the coordinator captures this before a
        duck so unduck restores what the user actually had, not a policy
        constant."""
        fn = getattr(self._observe_backend(), "current_volume", None)
        return fn(target) if fn else None

    def nominal_volume(self, target: Target = Target(name="local")):
        """The live backend's idea of a normal listening level, or None when it
        has none — the coordinator's last resort when no pre-duck reading is
        available. The two dials differ (Mopidy 0-100, the phone's mpv 0-170),
        so this must never be a single constant."""
        fn = getattr(self._observe_backend(), "nominal_volume", None)
        return fn(target) if fn else None
