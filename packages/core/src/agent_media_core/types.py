"""Core types shared across intake, route, render, sinks, and state.

The architecture is event-driven: any intake source produces an `Event`,
route applies policy, render produces audio if needed, sinks play it to a
target. See docs/reference/restructure.md for the full picture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol


class Source(str, Enum):
    """Where an event originated. New sources register here."""

    CLAUDE_CODE = "claude-code"
    CODEX = "codex"
    HERMES = "hermes"
    PI = "pi"
    OPENCODE = "opencode"
    HA_SSE = "ha-sse"
    HA_STT = "ha-stt"
    MATRIX = "matrix"
    WATCHER = "watcher"
    CLI = "cli"
    MCP = "mcp"


class Priority(str, Enum):
    """Drives pre-emption decisions in route/."""

    LOW = "low"            # ambient announcement; skip if anything's playing
    NORMAL = "normal"      # default agent response
    HIGH = "high"          # notifications, prompts
    URGENT = "urgent"      # alarms; interrupt and full volume


class ContentType(str, Enum):
    """Drives interruption strategy for what's currently in a music sink.

    Music ducks; longform (audiobook/podcast) pauses-and-resumes; dj-set
    ducks to preserve continuity; ambient ducks deeper.
    """

    MUSIC = "music"
    AUDIOBOOK = "audiobook"
    PODCAST = "podcast"
    DJ_SET = "dj-set"
    AMBIENT = "ambient"
    UNKNOWN = "unknown"


#: Target names retired by a rename, and what they are called now (27 Sep
#: 2026: `app` is Sasonica ABS, `next` is Sasonica). A name stored before the
#: rename — a history row, a saved choice, an env value — still resolves;
#: the per-target env keys (MEDIA_SPEECH_SOCKET_<TARGET> …) were renamed
#: with it and are read under the new name only.
RENAMED_TARGETS = {"app": "abs", "next": "sasonica"}


def target_name(name: str) -> str:
    """`name` as it is called now."""
    return RENAMED_TARGETS.get(name, name)


@dataclass(frozen=True)
class Target:
    """Where to play audio. Resolved by sink implementations.

    `name` is a logical identifier (local, snapcast-<room>, bt-car,
    matrix-room-<id>). Sinks know how to bind it. A retired name
    (`RENAMED_TARGETS`) becomes the current one.
    """

    name: str

    def __post_init__(self) -> None:
        if self.name in RENAMED_TARGETS:
            object.__setattr__(self, "name", RENAMED_TARGETS[self.name])


@dataclass(frozen=True)
class Event:
    """One unit of work submitted by an intake source."""

    text: str
    source: Source
    priority: Priority = Priority.NORMAL
    voice: Optional[str] = None       # render override; None = policy default
    engine: Optional[str] = None      # render override; None = policy default
    target: Optional[Target] = None   # sink override; None = policy default
    metadata: dict = field(default_factory=dict)  # source-specific extras


class Sink(Protocol):
    """Common contract for sink-speech and sink-music."""

    def play(self, uri: str, target: Target, **opts) -> None: ...
    def pause(self, target: Target) -> None: ...
    def resume(self, target: Target) -> None: ...
    def stop(self, target: Target) -> None: ...
    def duck(self, target: Target, level: int) -> None:
        """Set volume to `level` (0-100) for ducking; restore via unduck."""

    def unduck(self, target: Target) -> None: ...
    def position(self, target: Target) -> Optional[int]:
        """Current playback position in ms, or None if not playing."""
