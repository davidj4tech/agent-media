"""The radio's loop: agent_media_core.radio.tick, every few seconds.

Started with the canvas (as the search index is) and always running: an off
station is one small file read per pass, and a station started from a shell
(`media music radio`) is picked up without a word to this process. The Media
tab's own start and 👎 wake it at once (:func:`wake`).
"""

from __future__ import annotations

import sys
import threading

_PERIOD_S = 4.0
_WAKE = threading.Event()
_STARTED: list = [False]


def wake() -> None:
    _WAKE.set()


def _loop() -> None:
    from agent_media_core import radio

    while True:
        _WAKE.wait(_PERIOD_S)
        _WAKE.clear()
        try:
            radio.tick()
        except Exception as e:  # noqa: BLE001 — the loop outlives a bad pass
            print(f"radio: tick failed: {e}", file=sys.stderr)


def start() -> None:
    if _STARTED[0]:
        return
    _STARTED[0] = True
    threading.Thread(target=_loop, name="radio", daemon=True).start()
