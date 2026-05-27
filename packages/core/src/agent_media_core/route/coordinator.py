"""Speech ↔ music interruption coordinator.

When sink-speech is about to play a clip, the coordinator looks at what
sink-music is doing and applies the right interruption strategy (duck
or pause-and-resume) for the current content type. When speech ends, it
restores.

Replaces the polling-based `aar-mopidy-duck` daemon — event-driven,
content-type aware, observable via the state store.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from ..sinks.music import SinkMusic
from ..state import StateStore
from ..types import ContentType, Target
from . import _mpris
from .policy import (
    DEFAULT_POLICY,
    InterruptionPolicy,
    InterruptionStrategy,
    detect_content_type,
    policy_for,
)


def _env_duck_level() -> Optional[int]:
    """User override for the duck level. MEDIA_DUCK_VOLUME wins;
    legacy AAR_MOPIDY_DUCK_VOLUME falls through. Returns None when
    unset so the per-content-type policy default applies.
    """
    for var in ("MEDIA_DUCK_VOLUME", "AAR_MOPIDY_DUCK_VOLUME"):
        v = os.environ.get(var)
        if v:
            try:
                return max(0, min(100, int(v)))
            except ValueError:
                continue
    return None


log = logging.getLogger(__name__)


class Coordinator:
    """Mediates between sink-speech and sink-music.

    Stateless across instances — pass the same StateStore in if you want
    cross-process observability.
    """

    def __init__(self, *, music: Optional[SinkMusic] = None,
                 state: Optional[StateStore] = None,
                 music_target: Target = Target(name="local")) -> None:
        self.music = music or SinkMusic()
        self.state = state or StateStore()
        self.music_target = music_target
        self._mpris_paused: list[str] = []

    # ---- public API used by sink-speech --------------------------------

    def before_speech(self) -> None:
        """Apply interruption for whatever sink-music is currently
        playing. Records baseline volume + position so after_speech can
        restore.
        """
        # MPRIS: pause browser/external players regardless of Mopidy state.
        if _mpris.enabled():
            self._mpris_paused = _mpris.playing_players()
            _mpris.pause_players(self._mpris_paused)

        try:
            uri = self.music.now_playing_uri(self.music_target)
        except Exception as e:  # noqa: BLE001
            self._log_err("music: now_playing_uri failed", str(e))
            return
        if not uri:
            return  # nothing to interrupt via Mopidy

        content_type = detect_content_type(uri)
        policy = policy_for(content_type)
        extras: dict = {}

        if policy.strategy == InterruptionStrategy.PAUSE:
            try:
                pos_ms = self.music.position(self.music_target)
            except Exception:
                pos_ms = None
            extras["pause_pos_ms"] = pos_ms
            extras["lead_in_ms"] = policy.lead_in_ms
            extras["strategy"] = "pause"
            try:
                self.music.pause(self.music_target)
            except Exception as e:  # noqa: BLE001
                self._log_err("music: pause failed", str(e))
                return
        else:
            env_override = _env_duck_level()
            level = env_override if env_override is not None else policy.duck_level
            extras["strategy"] = "duck"
            extras["duck_level"] = level
            extras["baseline_volume"] = policy.baseline_volume
            try:
                self.music.duck(self.music_target, level)
            except Exception as e:  # noqa: BLE001
                self._log_err("music: duck failed", str(e))
                return

        self.state.set_now_playing(
            sink="music",
            uri=uri,
            started_at=time.time(),
            content_type=content_type.value if content_type else None,
            target=self.music_target.name,
            extras={"interruption": extras},
        )

    def after_speech(self) -> None:
        """Restore from whatever before_speech did."""
        if self._mpris_paused:
            _mpris.resume_players(self._mpris_paused)
            self._mpris_paused = []

        np = self.state.get_now_playing("music")
        if not np or not np.get("extras"):
            return
        interruption = (np["extras"] or {}).get("interruption") or {}
        strategy = interruption.get("strategy")

        try:
            if strategy == "pause":
                # Back up by the lead-in window so the listener doesn't
                # miss the word they were on when speech cut in. Best
                # effort — if seek fails or position wasn't captured,
                # we just resume from where pause landed.
                pos_ms = interruption.get("pause_pos_ms")
                lead_in_ms = int(interruption.get("lead_in_ms") or 0)
                if pos_ms is not None and lead_in_ms > 0:
                    try:
                        self.music.seek_cur(self.music_target,
                                            max(0, int(pos_ms) - lead_in_ms))
                    except Exception:
                        pass
                self.music.resume(self.music_target)
            elif strategy == "duck":
                baseline = int(interruption.get("baseline_volume") or 45)
                self.music.unduck(self.music_target, restore=baseline)
        except Exception as e:  # noqa: BLE001
            self._log_err(f"music: restore ({strategy}) failed", str(e))
        finally:
            # Whether restore succeeded or not, clear the marker so a
            # stuck row doesn't poison the next clip.
            self.state.clear_now_playing("music")

    # ---- helpers --------------------------------------------------------

    def _log_err(self, msg: str, detail: str) -> None:
        log.warning("%s: %s", msg, detail)
        try:
            self.state.log_error("coordinator", msg, extras={"detail": detail})
        except Exception:
            pass


__all__ = ["Coordinator", "InterruptionPolicy", "InterruptionStrategy",
           "DEFAULT_POLICY", "detect_content_type", "policy_for"]
