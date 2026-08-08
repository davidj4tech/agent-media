"""Cross-process breaker state for slow remote endpoints.

Speech is mostly produced by short-lived processes: the Claude Code Stop hook
spawns a detached child per reply, `media say` is one shot. An in-memory
breaker therefore starts cold every single time, so each utterance re-pays the
cost of discovering that the phone bridge is slow — which is the whole cost we
were trying to remove. Only the long-running media-mcp daemon benefited.

So the deadline is shared on disk: one small JSON file per namespace, holding
`{endpoint: unix-deadline}`. Wall-clock, not monotonic, because it crosses
processes. Reads are lazy and cached for the life of the process; writes happen
only when a breaker actually trips, which is rare.

Failure is always "no breaker": a missing, unreadable, or corrupt file just
means every call is attempted, exactly as before this existed.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ._paths import state_dir


def _path(namespace: str) -> Path:
    return state_dir() / f"breaker-{namespace}.json"


def load(namespace: str) -> dict[str, float]:
    """`{endpoint: unix-deadline}`, expired entries dropped."""
    try:
        raw = json.loads(_path(namespace).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    now = time.time()
    out: dict[str, float] = {}
    for k, v in raw.items():
        try:
            deadline = float(v)
        except (TypeError, ValueError):
            continue
        if deadline > now:
            out[str(k)] = deadline
    return out


def store(namespace: str, state: dict[str, float]) -> None:
    """Persist `state`, dropping anything already expired. Best-effort."""
    now = time.time()
    keep = {k: v for k, v in state.items() if v > now}
    path = _path(namespace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(keep))
        tmp.replace(path)                    # atomic: readers never see a partial file
    except OSError:
        pass


def clear(namespace: str) -> None:
    try:
        _path(namespace).unlink(missing_ok=True)
    except OSError:
        pass
