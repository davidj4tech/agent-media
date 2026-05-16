"""Best-effort desktop / Android notifications.

Wraps termux-notification on Termux; no-ops elsewhere. The intake
pipeline calls this on render fallback so silent degradation gets a
visible signal.

Throttled by `key` so the same engine failing in a loop doesn't drown
the notification shade.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional


log = logging.getLogger(__name__)

DEFAULT_THROTTLE_SECONDS = 600  # 10 min between notifications per key


def _stamp_dir() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME",
                                str(Path.home() / ".local" / "state")))
    d = state / "agent-media" / "notify"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _should_send(key: str, throttle: int) -> bool:
    stamp = _stamp_dir() / f"{key}.last"
    now = int(time.time())
    try:
        last = int(stamp.read_text().strip())
        if (now - last) < throttle:
            return False
    except (OSError, ValueError):
        pass
    try:
        stamp.write_text(str(now))
    except OSError:
        pass
    return True


def notify(*, key: str, title: str, content: str,
           throttle: int = DEFAULT_THROTTLE_SECONDS) -> bool:
    """Fire a notification with the host's best available channel.

    Returns True if a notification was actually dispatched (not throttled,
    not missing tooling).
    """
    if os.environ.get("MEDIA_NOTIFY_DISABLED") == "1":
        return False
    if not _should_send(key, throttle):
        return False
    if shutil.which("termux-notification"):
        try:
            # Termux:API's first call after the Android service has been
            # idle can take 10+ seconds to wake — give it room.
            subprocess.run(
                ["termux-notification",
                 "--id", f"agent-media:{key}",
                 "--title", title,
                 "--content", content,
                 "--priority", "high"],
                check=False, timeout=20,
            )
            return True
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("notify: termux-notification failed: %s", e)
            return False
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", title, content],
                           check=False, timeout=5)
            return True
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("notify: notify-send failed: %s", e)
            return False
    log.info("notify: no channel available (%s — %s)", title, content)
    return False
