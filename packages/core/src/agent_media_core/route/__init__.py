"""Route: policy + coordinator.

Decides what to do when a speech event arrives (duck music, pause an
audiobook, etc). Replaces the polling-based aar-mopidy-duck daemon.
"""

from .coordinator import Coordinator
from .policy import (
    DEFAULT_POLICY,
    InterruptionPolicy,
    InterruptionStrategy,
    detect_content_type,
    policy_for,
)

__all__ = [
    "Coordinator",
    "DEFAULT_POLICY",
    "InterruptionPolicy",
    "InterruptionStrategy",
    "detect_content_type",
    "policy_for",
]
