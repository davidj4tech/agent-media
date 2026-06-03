"""Channel-vs-channel concurrency: focus + bed.

Distinct from speech interruption (route/policy.InterruptionPolicy, which is
about *speech* cutting into a channel). This is about the **book** and
**music** channels sharing the speakers: bringing one to the front (`focus`)
puts the other into a quiet bed (duck) or out of the way (pause).

Which of those the *music* channel does under a foregrounded book is
switchable at runtime — instrumental can sit under narration as a bed, but
lyrics distract, so pause. That choice (`book_bed`) lives in the state store
so both these verbs and the speech coordinator agree on the arrangement.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from ..sinks.book import SinkBook
from ..sinks.music import SinkMusic
from ..state import StateStore
from ..types import ContentType, Target
from .policy import InterruptionStrategy, policy_for


FOCUS_BOOK = "book"
FOCUS_MUSIC = "music"

BED_DUCK = "duck"
BED_PAUSE = "pause"

_DEFAULT_BED_LEVEL = 12


def bed_level() -> int:
    """Music volume while bedded under a foregrounded book. Override with
    MEDIA_BED_LEVEL.
    """
    v = os.environ.get("MEDIA_BED_LEVEL")
    if v:
        try:
            return max(0, min(100, int(v)))
        except ValueError:
            pass
    return _DEFAULT_BED_LEVEL


def music_front_level() -> int:
    """Volume to restore the music channel to when it returns to the front —
    the same baseline the speech coordinator un-ducks to.
    """
    return policy_for(ContentType.MUSIC).baseline_volume


def bed_strategy(state: StateStore) -> str:
    """Current music-under-book behaviour (`duck` | `pause`)."""
    return state.get_book_bed() or BED_DUCK


@dataclass(frozen=True)
class ConcurrencyPolicy:
    """Resolved book↔music arrangement at a moment in time."""

    focus: Optional[str]
    bed: str

    @property
    def music_bedded_by_pause(self) -> bool:
        """True when the music channel is intentionally *paused* because a
        book is in front — the speech coordinator must then leave music
        alone rather than duck-and-resume it.
        """
        return self.focus == FOCUS_BOOK and self.bed == BED_PAUSE


def resolve(state: StateStore) -> ConcurrencyPolicy:
    return ConcurrencyPolicy(focus=state.get_focus(), bed=bed_strategy(state))


def apply_focus(channel: str, *, music: SinkMusic, book: SinkBook,
                state: StateStore,
                music_target: Target = Target(name="local"),
                book_target: Target = Target(name="local")) -> dict:
    """Bring `channel` to the front; push the other into its bed.

    `focus book`  → music to a quiet bed (duck) or out of the way (pause),
                    per `book_bed`; the book plays at full and resumes if
                    it was bedded.
    `focus music` → the book pauses (the caller saves its bookmark first);
                    music returns to full volume and resumes.
    """
    bed = bed_strategy(state)
    if channel == FOCUS_BOOK:
        if bed == BED_PAUSE:
            music.pause(music_target)
        else:
            music.duck(music_target, bed_level())
        book.set_volume(100, book_target)
        if not book.idle(book_target):
            book.resume(book_target)
        state.set_focus(FOCUS_BOOK)
        return {"focus": FOCUS_BOOK, "bed": bed, "bed_level": bed_level()}

    if channel == FOCUS_MUSIC:
        if not book.idle(book_target):
            book.pause(book_target)
        music.unduck(music_target, restore=music_front_level())
        music.resume(music_target)
        state.set_focus(FOCUS_MUSIC)
        return {"focus": FOCUS_MUSIC, "level": music_front_level()}

    raise ValueError(f"unknown focus channel {channel!r}")
