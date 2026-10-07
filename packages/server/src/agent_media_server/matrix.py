"""The Matrix `/sync` loop, run inside the canvas.

Step 0 of docs/proposals/2026-09-23-matrix-in-the-app.md: one loop per
token, here, with every reader subscribed to it — today the speech intake
(agent_media_intake_matrix), next the rooms on `/targets`. Started with the
canvas, as the radio is; idle unless `MATRIX_ACCESS_TOKEN` and
`MATRIX_ROOM_ALLOW` are set, and off with `MEDIA_MATRIX_SYNC=0` (for a host
still running the standalone `media-intake-matrix`, which must not share a
token with this one).
"""

from __future__ import annotations

import os
import sys

from agent_media_core import matrix

_SYNC: list = [None]


def sync() -> "matrix.Sync | None":
    """The running loop, for a reader to subscribe to; None when off."""
    return _SYNC[0]


def start() -> None:
    if _SYNC[0] is not None or os.environ.get("MEDIA_MATRIX_SYNC", "1") == "0":
        return
    config = matrix.Config.from_env()
    if config is None:
        return
    s = matrix.Sync(config)
    # The speech intake is its own package, installed alongside but not a
    # dependency of this one.
    try:
        import agent_media_intake_matrix as intake
    except ImportError:
        intake = None
    if intake is not None:
        on_event, _stop = intake.consumer(config)
        s.subscribe(on_event)
    _SYNC[0] = s
    s.start()
    print(f"matrix: sync on for {len(config.rooms)} room(s)"
          f"{', speech intake attached' if intake else ''}", file=sys.stderr)
