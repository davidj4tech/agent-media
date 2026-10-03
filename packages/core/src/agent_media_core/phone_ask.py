"""Ask the phone for something, and wait for David's answer (server-contract.md §6.21).

The agent's half of the phone as its eyes and hands: `POST /phone/ask` to
the server on this host with the host's own token (~/.amux/auth_token),
then long-poll `GET /phone/ask?id=` until the phone answers, David says no,
or the time runs out. Used by the `phone_ask` MCP tool. David, 1 Oct 2026
(docs/proposals/2026-10-01-the-phone-as-eyes-and-hands.md).

Statuses: `ok` (with `result`: a photo's `{path, width, height}`, a file
under ~/shared/ the agent reads with its own tools; a dnd's `{on, until}`),
`denied`, `timeout`,
`no_phone` (no phone that can do it is connected — said at once), `failed`
(with `error`), `cancelled`, `gone` (the server restarted), `error` (the
server could not be reached).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

#: Longest single poll; the server caps it at 60 s.
POLL_S = 30.0


def _token() -> str:
    tok = os.environ.get("AMUX_AUTH_TOKEN", "")
    if tok:
        return "" if tok.lower() == "none" else tok
    try:
        return (Path.home() / ".amux" / "auth_token").read_text().strip()
    except OSError:
        return ""


def _base() -> str:
    from .cli import _canvas_web_url

    return _canvas_web_url().rstrip("/")


def _ppid(pid: int) -> int:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # The command may hold spaces and parentheses: the fields after the
        # last ")" are fixed.
        return int(stat.rpartition(")")[2].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def session_of_caller(max_up: int = 8) -> str | None:
    """The Claude Code session this process runs under: the first ancestor
    Claude's own session record names (an MCP server is the claude's child,
    or a wrapper's). None when there is none — the ask still goes, unnamed."""
    from . import claude_sessions

    pid = os.getppid()
    for _ in range(max_up):
        if pid <= 1:
            return None
        sid = claude_sessions.session_for_pid(pid)
        if sid:
            return sid
        pid = _ppid(pid)
    return None


def _call(method: str, path: str, body: dict | None = None,
          timeout: float = 10.0) -> tuple[int, dict]:
    req = urllib.request.Request(
        _base() + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Auth-Token": _token(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def until_epoch(until: str, now: float | None = None) -> float | None:
    """When a `dnd` ends, as epoch seconds: "15:30" (this host's local time,
    the next one), "+90" or "90m" (minutes from now), "2h", or an ISO time.
    None when it cannot be read."""
    import datetime as dt
    import re

    raw = str(until or "").strip().lower()
    now = time.time() if now is None else now
    m = re.fullmatch(r"\+?(\d+(?:\.\d+)?)\s*(m|min|mins|minutes|h|hr|hrs|hours)?", raw)
    if m:
        n = float(m.group(1))
        return now + n * (3600 if (m.group(2) or "m").startswith("h") else 60)
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})\s*(am|pm)?", raw)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if m.group(3) == "pm" and hh < 12:
            hh += 12
        elif m.group(3) == "am" and hh == 12:
            hh = 0
        if hh > 23 or mm > 59:
            return None
        base = dt.datetime.fromtimestamp(now)
        at = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if at.timestamp() <= now:
            at += dt.timedelta(days=1)
        return at.timestamp()
    try:
        at = dt.datetime.fromisoformat(str(until).strip())
    except ValueError:
        return None
    return at.timestamp()


def _outcome(a: dict) -> dict:
    out = {"status": a.get("status")}
    for k in ("result", "error"):
        if a.get(k):
            out[k] = a[k]
    return out


def ask(kind: str, why: str, timeout_s: float = 300.0,
        session: str | None = None, until: str = "") -> dict:
    """Ask, then wait up to `timeout_s` (the ask's own time on the phone is
    the server's: 5 min for a photo, 2 for the others). `session` defaults
    to the caller's. A `dnd` needs `until` (until_epoch's forms)."""
    body = {"kind": kind, "why": why, "session": session or session_of_caller()}
    if kind == "dnd":
        at = until_epoch(until)
        if at is None:
            return {"status": "error",
                    "error": "until: a time like 15:30, or minutes like +90"}
        body["params"] = {"until": at}
    try:
        code, obj = _call("POST", "/phone/ask", body)
        if code != 200:
            return {"status": "error", "error": obj.get("error") or f"HTTP {code}"}
        a = obj["ask"]
        deadline = time.monotonic() + max(1.0, timeout_s)
        while a.get("status") == "open":
            left = deadline - time.monotonic()
            if left <= 0:
                _call("POST", "/phone/cancel", {"id": a["id"]})
                return {"status": "timeout"}
            wait = min(POLL_S, left)
            code, obj = _call("GET", f"/phone/ask?id={a['id']}&wait={wait:.0f}",
                              timeout=wait + 10)
            if code == 404:
                return {"status": "gone"}
            if code != 200:
                return {"status": "error", "error": obj.get("error") or f"HTTP {code}"}
            a = obj["ask"]
        return _outcome(a)
    except (OSError, ValueError, KeyError) as e:
        return {"status": "error", "error": str(e)}
