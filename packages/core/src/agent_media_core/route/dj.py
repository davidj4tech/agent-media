"""Ducking the way a DJ would: fades, and a depth that fits what is said.

The plain duck is one step down before the first word and one step up after
the last, the same for a two-word notice as for a two-minute answer. A DJ
rides the fader instead: the music slides under the voice as it starts and
comes back up after a beat, a station ident barely dips it, and a long talk
stops it altogether. This module decides that ("the cue") and moves the
volume ("the fader"); the coordinator still owns what is ducked and the debt
that says how to put it back.

Off unless ``MEDIA_DJ_DUCK`` is set (David, 20 Oct 2026: "instead of just
plain ducking … it works more like a DJ would"). Off means the coordinator
behaves exactly as before.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

from ..types import ContentType
from .policy import InterruptionStrategy, policy_for


def enabled() -> bool:
    """Is DJ-style ducking switched on (``MEDIA_DJ_DUCK``)?"""
    return (os.environ.get("MEDIA_DJ_DUCK") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def estimate_seconds(text: str) -> Optional[float]:
    """How long ``text`` takes to say, by the device renderer's own rate.

    A guess made before anything is rendered, which is when the cue has to be
    chosen. None for no text: an unknown length is not a short one.
    """
    text = (text or "").strip()
    if not text:
        return None
    from ..render.device import CHARS_PER_S, LEAD_S
    return LEAD_S + len(text) / CHARS_PER_S


@dataclass(frozen=True)
class Cue:
    """What to do to the music for one reply, decided before it starts."""

    strategy: InterruptionStrategy
    level: int              # duck level; 0 for a pause (faded out first)
    fade_down_ms: int
    fade_up_ms: int
    beat_ms: int            # silence after the last word before the fade up
    reason: str             # one word for the log and the now-playing row

    def as_extras(self) -> dict:
        return {"cue": self.reason, "fade_down_ms": self.fade_down_ms,
                "fade_up_ms": self.fade_up_ms, "beat_ms": self.beat_ms}


def plan_cue(content_type: Optional[ContentType], *, priority: str = "",
             ask: bool = False, est_s: Optional[float] = None) -> Cue:
    """Choose the cue for a reply over music of ``content_type``.

    Pure: everything it needs is passed in, so the rules can be read and
    tested as a table.

    - Longform (book, podcast) pauses as it always has: you cannot half-hear
      narration, and its lead-in rewind is what makes the resume work.
    - Ambient sinks deeper and moves slower: it is a bed, not a song.
    - A short notice (under ``MEDIA_DJ_SHORT_S``, 4 s) only dips the music.
    - A question, or a long reply (over ``MEDIA_DJ_LONG_S``, 45 s), stops the
      music: one wants an answer, the other is a talk, not a voice-over. A DJ
      set is never stopped — pausing a mix breaks it.
    - Urgent comes in fast.
    - Anything else is today's duck, with fades.
    """
    ct = content_type or ContentType.UNKNOWN
    base = policy_for(ct)
    down = _env_int("MEDIA_DJ_FADE_DOWN_MS", 400)
    up = _env_int("MEDIA_DJ_FADE_UP_MS", 1500)
    beat = _env_int("MEDIA_DJ_BEAT_MS", 300)
    if priority == "urgent":
        down = min(down, 150)

    if base.strategy == InterruptionStrategy.PAUSE:
        return Cue(InterruptionStrategy.PAUSE, 0, 0, 0, 0, "longform")
    if ct == ContentType.AMBIENT:
        return Cue(InterruptionStrategy.DUCK, base.duck_level,
                   max(down, 800), max(up, 2500), beat, "ambient")

    long_s = _env_float("MEDIA_DJ_LONG_S", 45.0)
    short_s = _env_float("MEDIA_DJ_SHORT_S", 4.0)
    if ct != ContentType.DJ_SET:
        if ask:
            return Cue(InterruptionStrategy.PAUSE, 0, down, up, beat, "question")
        if est_s is not None and est_s >= long_s:
            return Cue(InterruptionStrategy.PAUSE, 0, down, up, beat, "long")
    if (est_s is not None and est_s < short_s and priority != "urgent"):
        return Cue(InterruptionStrategy.DUCK,
                   _env_int("MEDIA_DJ_DIP_LEVEL", 25), down, up, beat, "short")
    return Cue(InterruptionStrategy.DUCK, base.duck_level, down, up, beat,
               "urgent" if priority == "urgent" else "talk")


def _curve(t: float) -> float:
    """Smoothstep: eases in and out, so neither end of the fade is a jump."""
    return t * t * (3 - 2 * t)


def fade(set_volume: Callable[[int], None], start: int, end: int, ms: int, *,
         step_ms: int = 40, sleep: Callable[[float], None] = time.sleep) -> None:
    """Move a volume from ``start`` to ``end`` over ``ms``, one setting per
    ``step_ms``, always finishing exactly on ``end``.

    Paced by the clock, not by counting sleeps: a step that is slow to land (a
    round trip to Mopidy or the snapserver) is not added to the fade, the next
    one is simply further along. Blocking, so a caller that has to be quick
    must not ask for a long fade.
    """
    start, end = int(start), int(end)
    if ms <= 0 or start == end:
        set_volume(end)
        return
    began = time.monotonic()
    last: Optional[int] = None
    while True:
        t = (time.monotonic() - began) * 1000.0 / ms
        if t >= 1.0:
            break
        v = int(round(start + (end - start) * _curve(t)))
        if v != last:
            set_volume(v)
            last = v
        sleep(step_ms / 1000.0)
    set_volume(end)


__all__ = ["Cue", "enabled", "estimate_seconds", "fade", "plan_cue"]
