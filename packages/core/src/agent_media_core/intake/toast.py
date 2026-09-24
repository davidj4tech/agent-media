"""Hold a reply until the listener asks for it — the tmux toast (prototype).

A reply from the pane the listener is looking at speaks at once. A reply from
any other pane, while someone is at the desk (a client typed into within
MEDIA_TOAST_PRESENCE_S, default 300), is held instead: a toast in the
status line says it is ready, `prefix y` plays it and `prefix Y` drops it. The
phone's version of the same rule is a tap on the notification; this is the
desk's.

Off unless MEDIA_TOAST_GATE=1. Nobody at the desk means the reply takes the
usual path (the phone lane) and is not held.

A held reply is rendered and archived at once, like a muted pane's
(`extras.held`), so it is in the transcript straight away — the app shows it
unheard, with a big Play — and playing it is a replay of that history row,
which marks it heard. What the toast keeps is only which row it is waiting
on: JSON files under state_dir()/toast-pending, newest last. A toast older
than MEDIA_TOAST_TTL seconds (default 1800) is dropped; the reply stays in
the transcript, still unheard.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from .._paths import state_dir
from ..types import Event

log = logging.getLogger(__name__)


def gate_enabled() -> bool:
    return os.environ.get("MEDIA_TOAST_GATE", "0") == "1"


def _ttl() -> int:
    try:
        return int(os.environ.get("MEDIA_TOAST_TTL", "1800"))
    except ValueError:
        return 1800


def _tmux(args: list[str]) -> str:
    try:
        out = subprocess.run(["tmux", *args], capture_output=True, text=True,
                             timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _presence_s() -> int:
    try:
        return int(os.environ.get("MEDIA_TOAST_PRESENCE_S", "300"))
    except ValueError:
        return 300


def _desk() -> Optional[tuple[str, str]]:
    """(client, pane it shows) for the client typed into most recently, if
    that was within MEDIA_TOAST_PRESENCE_S; else None — nobody at the desk.

    Attached is not present: mosh and ssh clients stay attached for days after
    the listener has walked off, so keystroke recency is the signal.
    """
    out = _tmux(["list-clients", "-F", "#{client_activity} #{client_name} #{pane_id}"])
    best = None
    for line in out.splitlines():
        try:
            ts, name, pane = line.split(" ", 2)
            ts_i = int(ts)
        except ValueError:
            continue
        if best is None or ts_i > best[0]:
            best = (ts_i, name, pane)
    if best is None or time.time() - best[0] > _presence_s():
        return None
    return best[1], best[2]


def _pending_dir() -> Path:
    d = state_dir() / "toast-pending"
    d.mkdir(parents=True, exist_ok=True)
    return d


def should_hold() -> bool:
    """Hold this reply? Only with the gate on, from a tmux pane, with someone
    at the desk looking at a different pane."""
    if not gate_enabled():
        return False
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return False
    desk = _desk()
    return desk is not None and desk[1] != pane


def _where(pane: str) -> str:
    return _tmux(["display-message", "-p", "-t", pane,
                  "#{session_name}:#{window_index} #{window_name}"]) or pane


def hold(event: Event) -> None:
    """Render and archive `event` unplayed, and put up the toast."""
    pane = os.environ.get("TMUX_PANE") or ""
    record = {
        "held_at": time.time(),
        "pane": pane,
        "where": _where(pane) if pane else "",
        # The hook's own dedup key (`_play_now`): how play finds the row.
        "key": hashlib.sha1(event.text.encode("utf-8")).hexdigest(),
        "text": event.text[:200],
    }
    path = _pending_dir() / f"{time.time_ns()}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record))
    tmp.rename(path)
    log.info("toast: held a reply from %s", record["where"])
    show(record["where"])
    event.metadata["held"] = True
    from .hook_claude_code import _play_detached

    _play_detached(event)


def _pending() -> list[Path]:
    """Live held replies, oldest first; expired ones are removed."""
    cutoff = time.time() - _ttl()
    live = []
    for p in sorted(_pending_dir().glob("*.json")):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                continue
        except OSError:
            continue
        live.append(p)
    return live


def show(where: str) -> None:
    n = len(_pending())
    more = f" (+{n - 1} more)" if n > 1 else ""
    msg = f"🔊 {where}: reply ready{more} — prefix y play · prefix Y dismiss"
    ms = os.environ.get("MEDIA_TOAST_MS", "10000")
    desk = _desk()
    if desk:
        _tmux(["display-message", "-c", desk[0], "-d", ms, msg])


def _row_for(key: str) -> Optional[int]:
    """The history row the held reply was archived as, or None (yet)."""
    from ..state import StateStore

    for row in StateStore().recent_history(sink="speech", limit=200):
        ex = row.get("extras")
        if isinstance(ex, dict) and ex.get("held") and ex.get("dedup_key") == key:
            return int(row["id"])
    return None


def _wait_for_row(key: str) -> Optional[int]:
    """A reply held a moment ago may still be rendering: wait for its row."""
    deadline = time.time() + float(os.environ.get("MEDIA_TOAST_RENDER_WAIT_S", "30"))
    while True:
        rid = _row_for(key)
        if rid is not None or time.time() >= deadline:
            return rid
        time.sleep(0.5)


def _take_newest() -> Optional[Path]:
    live = _pending()
    return live[-1] if live else None


def _after_take() -> None:
    """Re-toast for whatever is still waiting, so a second reply is not lost."""
    live = _pending()
    if live:
        try:
            where = json.loads(live[-1].read_text()).get("where") or ""
        except (OSError, ValueError):
            where = ""
        show(where)


def _say(msg: str, ms: str = "2000") -> None:
    desk = _desk()
    if desk:
        _tmux(["display-message", "-c", desk[0], "-d", ms, msg])


def play() -> int:
    """Play the newest held reply. 0 if one was played, 1 if not."""
    path = _take_newest()
    if path is None:
        _say("🔇 no reply waiting")
        return 1
    try:
        key = json.loads(path.read_text()).get("key") or ""
    except (OSError, ValueError):
        key = ""
    try:
        path.unlink()
    except OSError:
        pass
    rid = _wait_for_row(key) if key else None
    _after_take()
    if rid is None:
        _say("🔇 that reply never rendered")
        return 1
    from ..cli import main as media

    return media(["replay", "--id", str(rid)])


def dismiss() -> int:
    path = _take_newest()
    if path is None:
        return 1
    try:
        path.unlink()
    except OSError:
        pass
    _after_take()
    return 0


def listing() -> list[dict]:
    out = []
    for p in _pending():
        try:
            r = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        out.append({"id": p.stem, "where": r.get("where", ""),
                    "age_s": int(time.time() - r.get("held_at", time.time())),
                    "text": r.get("text", "")[:80]})
    return out
