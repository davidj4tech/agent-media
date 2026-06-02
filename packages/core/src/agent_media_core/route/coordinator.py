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
import threading
from typing import Optional

from ..sinks.music import SinkMusic
from ..state import StateStore
from ..types import ContentType, Target
from . import _android, _mpris
from .policy import (
    DEFAULT_POLICY,
    InterruptionPolicy,
    InterruptionStrategy,
    coerce_content_type,
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
        self._mpris_remote_paused: dict[str, list[str]] = {}
        self._remote_pause_done: Optional[threading.Event] = None
        # Hosts where we sent a media-button play-pause that we need to undo
        # after speech finishes (Android phones via SSH).
        self._android_paused: list[str] = []

    # ---- public API used by sink-speech --------------------------------

    def pre_pause_remote(self) -> None:
        """Start remote MPRIS detect-and-pause in a background thread.

        Call this before render_text so the ~4.8s SSH cold-connect overlaps
        with rendering.  before_speech() waits up to 6s for it to finish
        before playing audio.
        """
        mpris_hosts = _mpris.ssh_hosts() if _mpris.enabled() else []
        android_hosts = _android.pause_hosts()
        if not mpris_hosts and not android_hosts:
            return
        self._remote_pause_done = threading.Event()

        def _work() -> None:
            for host in mpris_hosts:
                try:
                    remote = _mpris.remote_playing_players(host)
                    self._mpris_remote_paused[host] = remote
                    _mpris.pause_remote(host, remote)
                except Exception:  # noqa: BLE001
                    pass
            for host in android_hosts:
                try:
                    if _android.pause_for_speech(host):
                        self._android_paused.append(host)
                except Exception:  # noqa: BLE001
                    pass
            self._remote_pause_done.set()

        threading.Thread(target=_work, daemon=True).start()

    def before_speech(self) -> None:
        """Apply interruption for whatever sink-music is currently
        playing. Records baseline volume + position so after_speech can
        restore.
        """
        # Local MPRIS: pause browser/external players on this host.
        if _mpris.enabled():
            self._mpris_paused = _mpris.playing_players()
            _mpris.pause_players(self._mpris_paused)

        # Remote MPRIS + Android media-button: wait for pre_pause_remote()
        # background work to finish, or do it synchronously if it was never
        # started.
        mpris_hosts = _mpris.ssh_hosts() if _mpris.enabled() else []
        android_hosts = _android.pause_hosts()
        if mpris_hosts or android_hosts:
            if self._remote_pause_done is not None:
                self._remote_pause_done.wait(timeout=14)
            else:
                for host in mpris_hosts:
                    remote = _mpris.remote_playing_players(host)
                    self._mpris_remote_paused[host] = remote
                    _mpris.pause_remote(host, remote)
                for host in android_hosts:
                    if _android.pause_for_speech(host):
                        self._android_paused.append(host)

        try:
            uri = self.music.now_playing_uri(self.music_target)
        except Exception as e:  # noqa: BLE001
            self._log_err("music: now_playing_uri failed", str(e))
            return
        if not uri:
            return  # nothing to interrupt via Mopidy

        content_type = self._content_type_for(uri)
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
        for host, names in self._mpris_remote_paused.items():
            _mpris.resume_remote(host, names)
        self._mpris_remote_paused = {}
        for host in self._android_paused:
            try:
                _android.resume(host)
            except Exception:  # noqa: BLE001
                pass
        self._android_paused = []

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

    def _content_type_for(self, uri: str) -> ContentType:
        """Content type for what's playing on the music sink.

        Prefers the caller's queue-time intent (set by music_play, e.g.
        "this yt: URL is an audiobook") over re-sniffing the URI, since a
        YouTube/HTTP URL is otherwise indistinguishable from music and would
        be ducked rather than paused. Falls back to URI detection when no
        intent is recorded.

        The intent is trusted whenever present rather than matched against
        `uri`: Mopidy normalises a queued `yt:https://...` URL into a
        `yt:video:<id>` form by playback time, so an exact match would
        defeat the very case this exists for. The trade-off is that music
        started outside music_play (e.g. the /music skill talking straight
        to MPD) can leave a stale audiobook intent — that over-pauses music,
        which is benign and self-heals on the next music_play or music_stop.
        """
        try:
            intent = self.state.get_music_intent()
        except Exception:  # noqa: BLE001
            intent = None
        if intent:
            ct = coerce_content_type(intent.get("content_type"))
            if ct is not None:
                return ct
        return detect_content_type(uri)

    def _log_err(self, msg: str, detail: str) -> None:
        log.warning("%s: %s", msg, detail)
        try:
            self.state.log_error("coordinator", msg, extras={"detail": detail})
        except Exception:
            pass


__all__ = ["Coordinator", "InterruptionPolicy", "InterruptionStrategy",
           "DEFAULT_POLICY", "detect_content_type", "policy_for"]
