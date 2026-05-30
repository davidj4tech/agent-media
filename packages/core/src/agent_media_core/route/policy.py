"""Routing policy: content-type detection + interruption strategy.

Policy lives here so route/coordinator.py stays a thin orchestrator.

Content-type rules:
  - `podcast:` / `audiobook:` URI schemes → LONGFORM (pause-and-resume)
  - `yt:`, `local:`, `file://` → MUSIC by default; can be overridden
    per-track at queue-time via extras.
  - everything else → UNKNOWN, treated as MUSIC (duck).

Interruption rules:
  MUSIC      → duck to `duck_level` while speech plays
  AUDIOBOOK  → pause, resume at saved position +/- lead-in window
  PODCAST    → pause, resume at saved position +/- lead-in window
  DJ_SET     → duck (continuous content; pausing breaks vibe)
  AMBIENT    → duck deeper (DEEPER_DUCK_LEVEL)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..types import ContentType


class InterruptionStrategy(str, Enum):
    DUCK = "duck"
    PAUSE = "pause"


@dataclass(frozen=True)
class InterruptionPolicy:
    strategy: InterruptionStrategy
    # Default duck level for MUSIC was 18; bumped down to 10 because
    # the previous floor wasn't aggressive enough to keep speech
    # intelligible over louder mixes. Override via MEDIA_DUCK_VOLUME
    # (or legacy AAR_MOPIDY_DUCK_VOLUME) — read at coordinator time.
    duck_level: int = 10
    deeper_duck_level: int = 4          # ambient — even quieter
    lead_in_ms: int = 500               # pre-roll restored before pause-resume
    lead_out_ms: int = 500              # post-roll preserved before resuming
    baseline_volume: int = 45           # restored after un-duck if user
                                        # hasn't touched the dial


DEFAULT_POLICY = {
    ContentType.MUSIC:     InterruptionPolicy(InterruptionStrategy.DUCK),
    ContentType.DJ_SET:    InterruptionPolicy(InterruptionStrategy.DUCK),
    ContentType.AMBIENT:   InterruptionPolicy(InterruptionStrategy.DUCK,
                                              duck_level=4),
    ContentType.AUDIOBOOK: InterruptionPolicy(InterruptionStrategy.PAUSE),
    ContentType.PODCAST:   InterruptionPolicy(InterruptionStrategy.PAUSE),
    ContentType.UNKNOWN:   InterruptionPolicy(InterruptionStrategy.DUCK),
}


def policy_for(content_type: Optional[ContentType]) -> InterruptionPolicy:
    return DEFAULT_POLICY.get(content_type or ContentType.UNKNOWN,
                              DEFAULT_POLICY[ContentType.UNKNOWN])


# --- Decision 4C: sink-naming convention -----------------------------------
#
# Movies on sp4r play through its *default* pulse sink; the whole-house
# music/agent feed plays through the `am` / `am-music` sinks. Ducking a
# movie's dialogue is wrong — we want pause-and-resume. So the producing
# sink's *name* decides duck vs pause, independent of content type:
#
#   sink in {am, am-music}  → DUCK   (continuous house audio)
#   any other sink          → PAUSE  (default sink = movies/players)
#
# This is a policy layer; the *mechanism* that actually pauses a movie
# (mpv IPC or `pactl` cork on the producing sink) is separate and applied
# by the coordinator once it can observe the producing sink. Today the
# coordinator only drives the Mopidy music sink (always duckable), so this
# convention only changes behaviour for the future movie/default-sink path.

DUCKABLE_SINKS: tuple[str, ...] = ("am", "am-music")


def duckable_sinks() -> tuple[str, ...]:
    """Sink names whose audio is duckable. Override with the comma list
    `MEDIA_DUCKABLE_SINKS` (e.g. during the aar→am rename transition:
    `am,am-music,aar,aar-music`).
    """
    env = os.environ.get("MEDIA_DUCKABLE_SINKS")
    if env:
        names = tuple(s.strip() for s in env.split(",") if s.strip())
        if names:
            return names
    return DUCKABLE_SINKS


def strategy_for_sink(sink_name: Optional[str]) -> Optional[InterruptionStrategy]:
    """Decision 4C, raw: duck on a duckable sink, pause on any *named*
    non-duckable sink. Returns None when the sink is unknown, so callers
    fall back to content-type policy.
    """
    if sink_name is None:
        return None
    if sink_name in duckable_sinks():
        return InterruptionStrategy.DUCK
    return InterruptionStrategy.PAUSE


_PAUSE_POLICY = InterruptionPolicy(InterruptionStrategy.PAUSE)


def resolve_policy(content_type: Optional[ContentType],
                   sink_name: Optional[str] = None) -> InterruptionPolicy:
    """Combine the 4C sink convention with content-type policy.

    A *named* non-duckable sink (the default sink → movies) forces PAUSE.
    Otherwise content type decides — which is the only signal we have for
    audio on the duckable house sinks, and the behaviour when the sink is
    unknown (`sink_name=None`).
    """
    if strategy_for_sink(sink_name) is InterruptionStrategy.PAUSE:
        return _PAUSE_POLICY
    return policy_for(content_type)


def coerce_content_type(value: Optional[str]) -> Optional[ContentType]:
    """Parse a free-text content-type label into a `ContentType`.

    Accepts the enum values (`music`, `audiobook`, `podcast`, `dj-set`,
    `ambient`, `unknown`) case-insensitively, plus the `dj_set` underscore
    spelling. Returns None for anything unrecognised so callers can fall
    back to URI-based detection instead of failing.
    """
    if not value:
        return None
    v = value.strip().lower().replace("_", "-")
    try:
        return ContentType(v)
    except ValueError:
        return None


def detect_content_type(uri: Optional[str], *,
                        hint: Optional[ContentType] = None) -> ContentType:
    """Pick a content type from a Mopidy URI / hint.

    Explicit `hint` always wins (caller's right to override at queue time).
    """
    if hint is not None:
        return hint
    if not uri:
        return ContentType.UNKNOWN
    u = uri.lower()
    if u.startswith("podcast:"):
        return ContentType.PODCAST
    if u.startswith("audiobook:"):
        return ContentType.AUDIOBOOK
    if u.startswith(("yt:", "youtube:", "soundcloud:", "spotify:",
                     "local:", "file://", "http://", "https://")):
        return ContentType.MUSIC
    return ContentType.UNKNOWN
