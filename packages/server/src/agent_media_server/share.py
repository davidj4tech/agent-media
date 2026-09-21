"""A link shared to the app, played by agent-media (`/share`).

Moved out of the canvas's reply.py.
"""

from __future__ import annotations

import threading

from . import auth


def share_from_app(text: str, channel: str, bearer: str) -> tuple[bool, dict]:
    """A link shared to the app, played by agent-media. Gated like `/reply`.

    The same pipeline as media-share's `/share` (share.share, then the
    listener's dispatch on a thread): the verdict comes back for the toast
    while the fetching happens behind it.
    """
    import threading

    from agent_media_core import share as sharemod
    from agent_media_core.entrypoints import share_listener

    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if not (text or "").strip():
        return False, {"error": "nothing shared"}
    channel = channel if channel in ("music", "book") else ""
    try:
        url, verdict = sharemod.share(text, channel=channel,
                                      probe_timeout=share_listener.PROBE_TIMEOUT_S)
    except sharemod.ShareError as e:
        return False, {"error": str(e), "status": 422}
    threading.Thread(target=share_listener._play, args=(url, verdict, ""),
                     daemon=True, name="share-dispatch").start()
    return True, {"url": url, "channel": verdict.channel, "title": verdict.title,
                  "line": verdict.line()}
