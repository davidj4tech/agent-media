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


@dataclass(frozen=True)
class Target:
    """Where to play audio. Resolved by sink implementations.

    `name` is a logical identifier (local, snapcast-mel, snapcast-sp4r,
    bt-car, matrix-room-<id>). Sinks know how to bind it.
    """

    name: str


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
