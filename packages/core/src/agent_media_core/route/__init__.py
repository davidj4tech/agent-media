"""Route: policy + coordinator.

Decides what to do when a speech event arrives (duck music, pause an
audiobook, etc). Replaces the polling-based aar-mopidy-duck daemon.
"""

from .concurrency import (
    BED_DUCK,
    BED_PAUSE,
    FOCUS_BOOK,
    FOCUS_MUSIC,
    apply_focus,
    bed_strategy,
    resolve,
)
from .coordinator import Coordinator
from .policy import (
    DEFAULT_POLICY,
    DUCKABLE_SINKS,
    InterruptionPolicy,
    InterruptionStrategy,
    coerce_content_type,
    detect_content_type,
    duckable_sinks,
    policy_for,
    resolve_policy,
    strategy_for_sink,
)

__all__ = [
    "Coordinator",
    "DEFAULT_POLICY",
    "DUCKABLE_SINKS",
    "InterruptionPolicy",
    "InterruptionStrategy",
    "BED_DUCK",
    "BED_PAUSE",
    "FOCUS_BOOK",
    "FOCUS_MUSIC",
    "apply_focus",
    "bed_strategy",
    "coerce_content_type",
    "detect_content_type",
    "duckable_sinks",
    "policy_for",
    "resolve",
    "resolve_policy",
    "strategy_for_sink",
]
