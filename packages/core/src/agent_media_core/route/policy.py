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
    duck_level: int = 18                # mpd setvol when ducking
    deeper_duck_level: int = 8          # ambient
    lead_in_ms: int = 500               # pre-roll restored before pause-resume
    lead_out_ms: int = 500              # post-roll preserved before resuming
    baseline_volume: int = 45           # restored after un-duck if user
                                        # hasn't touched the dial


DEFAULT_POLICY = {
    ContentType.MUSIC:     InterruptionPolicy(InterruptionStrategy.DUCK),
    ContentType.DJ_SET:    InterruptionPolicy(InterruptionStrategy.DUCK),
    ContentType.AMBIENT:   InterruptionPolicy(InterruptionStrategy.DUCK,
                                              duck_level=8),
    ContentType.AUDIOBOOK: InterruptionPolicy(InterruptionStrategy.PAUSE),
    ContentType.PODCAST:   InterruptionPolicy(InterruptionStrategy.PAUSE),
    ContentType.UNKNOWN:   InterruptionPolicy(InterruptionStrategy.DUCK),
}


def policy_for(content_type: Optional[ContentType]) -> InterruptionPolicy:
    return DEFAULT_POLICY.get(content_type or ContentType.UNKNOWN,
                              DEFAULT_POLICY[ContentType.UNKNOWN])


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
