"""Route: policy + coordinator.

Decides what to do when a speech event arrives (duck music, pause an
audiobook, etc). Replaces the polling-based aar-mopidy-duck daemon.
"""

from .coordinator import Coordinator
from .policy import (
    DEFAULT_POLICY,
    DUCKABLE_SINKS,
    InterruptionPolicy,
    InterruptionStrategy,
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
    "detect_content_type",
    "duckable_sinks",
    "policy_for",
    "resolve_policy",
    "strategy_for_sink",
]
