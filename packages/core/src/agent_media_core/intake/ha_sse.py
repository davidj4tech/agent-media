"""Home Assistant SSE → intake daemon.

Subscribes to HA's event stream filtered to ``openclaw_message_received``,
extracts the message text, strips light markdown, and submits as an
Event(source=HA_SSE). Runs as a long-lived process so it belongs as a
runit service rather than a one-shot hook.

Replaces the bash bridge at
``packages/audio-relay/src/agent_audio_relay/shell/hooks/ha-tts-bridge.sh``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from typing import Optional

from ..types import Event, Priority, Source
from ._text import strip_markdown
from .submit import submit_event


log = logging.getLogger(__name__)

# Max characters to feed the renderer — matches the legacy bridge.
MAX_LENGTH = 3750
# Backoff bounds when the SSE connection drops.
BACKOFF_MIN = 1.0
BACKOFF_MAX = 30.0


_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")  # collapse markdown links
_running = True


def _shutdown(*_: object) -> None:
    global _running
    _running = False


def _clean(text: str) -> str:
    if not text:
        return ""
    out = _LINK_RE.sub(r"\1", text)
    out = strip_markdown(out)
    if len(out) > MAX_LENGTH:
        out = out[:MAX_LENGTH] + "..."
    return out.strip()


def _open_stream(url: str, token: str) -> urllib.request.addinfourl:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
        },
    )
    return urllib.request.urlopen(req, timeout=None)


def _consume(resp: urllib.request.addinfourl) -> None:
    """Read SSE events until the stream closes. Each `data:` JSON
    payload with a non-empty `data.message` becomes one submitted event.
    """
    for raw in resp:
        if not _running:
            return
        line = raw.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
        if not line.startswith("data:"):
            continue
        body = line[len("data:"):].strip()
        if not body or not body.startswith("{"):
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            continue
        msg = ((payload.get("data") or {}).get("message") or "").strip()
        text = _clean(msg)
        if not text:
            continue
        try:
            submit_event(Event(
                text=text,
                source=Source.HA_SSE,
                priority=Priority.HIGH,
                metadata={"kind": "announce",
                          "event": payload.get("event_type")
                                   or "openclaw_message_received"},
            ))
        except Exception as e:  # noqa: BLE001
            log.warning("ha-sse: submit failed: %s", e)


def main() -> int:
    if os.environ.get("MEDIA_HOOK_ENABLED", "1") == "0":
        return 0

    ha_url = os.environ.get("HA_URL", "http://127.0.0.1:8123").rstrip("/")
    token = os.environ.get("HA_TOKEN")
    if not token:
        print("ha-sse: HA_TOKEN not set", file=sys.stderr)
        return 2

    event_type = os.environ.get("HA_SSE_EVENT_TYPE", "openclaw_message_received")
    url = f"{ha_url}/api/stream?restrict={event_type}"

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("ha-sse: connecting to %s", url)

    backoff = BACKOFF_MIN
    while _running:
        try:
            with _open_stream(url, token) as resp:
                log.info("ha-sse: connected")
                backoff = BACKOFF_MIN
                _consume(resp)
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            if not _running:
                break
            log.warning("ha-sse: connection lost (%s), retrying in %.1fs",
                        e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
        except Exception as e:  # noqa: BLE001
            log.exception("ha-sse: unexpected error: %s", e)
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)

    log.info("ha-sse: shutting down")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(main())
