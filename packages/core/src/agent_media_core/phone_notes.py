"""Put a notification in the phone's shade through the canvas, not over ssh.

Roadmap item 15 (#5): `POST /notes` on this host's canvas (server notes.py,
contract §6.23) hands the note to the phone over the stream the phone keeps
open, so nothing here dials into the phone. The answer says whether a phone
took it now (`listening`) and whether any app here has ever shown notes
(`seen`); callers keep their old `ssh termux-notification` path for
"never" — an app build without notes — and only for that.

`MEDIA_PHONE_NOTES=0` skips this and goes straight to the old path.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional

from .phone_run import _base, _token


def enabled() -> bool:
    return os.environ.get("MEDIA_PHONE_NOTES", "1") not in ("0", "false", "no")


def _call(route: str, body: dict, timeout: float) -> Optional[dict]:
    req = urllib.request.Request(
        _base() + route, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Auth-Token": _token()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            got = json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return got if isinstance(got, dict) and got.get("ok") else None


def post(nid: str, title: str, text: str, *, priority: str = "default",
         ttl_s: float = 3600.0, keep: bool = False,
         timeout: float = 5.0) -> Optional[dict]:
    """The server's answer (`listening`, `seen`), or None when this path is
    off or the canvas did not take it."""
    if not enabled():
        return None
    return _call("/notes", {"id": nid, "title": title, "text": text,
                            "priority": priority, "ttl_s": ttl_s, "keep": keep},
                 timeout)


def clear(nid: str, timeout: float = 5.0) -> Optional[dict]:
    if not enabled():
        return None
    return _call("/notes/clear", {"id": nid}, timeout)


def usable(answer: Optional[dict]) -> bool:
    """Did the note path take it for a phone that shows notes?"""
    return bool(answer and answer.get("seen"))
