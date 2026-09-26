"""Hold a Normal reply until the listener asks for it (speak_priority.py).

A reply from a conversation someone is looking at speaks at once: its thread
on screen in the app, or its pane the one shown by the desk client typed into
most recently (within MEDIA_TOAST_LOOKING_S, default 1800). Any other is held.
With someone at the desk (a keystroke within MEDIA_TOAST_PRESENCE_S, 300), a toast in the status line says it is ready,
`prefix y` plays it and `prefix Y` drops it; with nobody there, it just waits
unheard in the app with its Play.

A held reply is rendered and archived at once, like a muted pane's
(`extras.held`), so it is in the transcript straight away — the app shows it
unheard, with a big Play — and playing it is a replay of that history row,
which marks it heard. What the toast keeps is only which row it is waiting
on: JSON files under state_dir()/toast-pending, newest last. A toast older
than MEDIA_TOAST_TTL seconds (default 21600) is dropped; the reply stays in
the transcript, still unheard.

Opening the conversation in the app plays its newest waiting reply, if it has
never been played (`take_for_opened`, called by the server's thread stream).
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


def _ttl() -> int:
    try:
        return int(os.environ.get("MEDIA_TOAST_TTL", "21600"))
    except ValueError:
        return 21600


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


def _looking_s() -> int:
    try:
        return int(os.environ.get("MEDIA_TOAST_LOOKING_S", "1800"))
    except ValueError:
        return 1800


def _desk(within: Optional[int] = None) -> Optional[tuple[str, str]]:
    """(client, pane it shows) for the client typed into most recently, if
    that was within `within` seconds (MEDIA_TOAST_PRESENCE_S by default);
    else None — nobody at the desk.

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
    if best is None or time.time() - best[0] > (_presence_s() if within is None else within):
        return None
    return best[1], best[2]


def _pending_dir() -> Path:
    d = state_dir() / "toast-pending"
    d.mkdir(parents=True, exist_ok=True)
    return d


def should_hold(session: str = "") -> bool:
    """Hold this Normal reply? Yes unless someone is looking at its
    conversation: on screen in the app (core's watching.py), or its pane is
    the one the desk's freshest client shows. That client may have been idle
    for MEDIA_TOAST_LOOKING_S (default 1800), longer than presence: a long
    turn is watched without a keystroke."""
    from .. import watching

    if watching.is_open(session):
        return False
    pane = os.environ.get("TMUX_PANE")
    desk = _desk(_looking_s()) if pane else None
    return desk is None or desk[1] != pane


def _where(pane: str) -> str:
    return _tmux(["display-message", "-p", "-t", pane,
                  "#{session_name}:#{window_index} #{window_name}"]) or pane


def remember(event: Event, ask: bool = False, *, key: str = "",
             where: str = "") -> None:
    """Put up the toast for `event`, which the caller renders held
    (`metadata["held"]`): what `play` and `take_for_opened` find it by.
    `ask`: it is a question's read-out, dropped once answered (`drop_asks`).
    `key` and `where` are for a caller with no pane (`media say --hold` from
    a timer): the row's key it will look the row up by, and the toast's label."""
    pane = os.environ.get("TMUX_PANE") or ""
    key = key or hashlib.sha1(event.text.encode("utf-8")).hexdigest()
    record = {
        "held_at": time.time(),
        "pane": pane,
        "session": (event.metadata or {}).get("session") or "",
        "where": where or (_where(pane) if pane else ""),
        # The row's dedup key (`_play_now` sets it on a reply, the ask path
        # on a question): how play finds the row.
        "key": key,
        "text": event.text[:200],
        "ask": ask,
    }
    path = _pending_dir() / f"{time.time_ns()}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record))
    tmp.rename(path)
    log.info("toast: held a %s from %s", "question" if ask else "reply", record["where"])
    show(record["where"])
    event.metadata["held"] = True
    event.metadata["dedup_key"] = key
    chime_held(record["session"])


def chime_held(session: str = "") -> bool:
    """The `held` earcon: a reply is waiting behind a Play. True if played.

    The toast only reaches someone at the desk; this reaches the room. But
    only a quiet room. Speech that is live already has the listener's
    attention (and the toast is up anyway), so a tone over it would be the
    one thing a held reply exists not to do — talk over something. So the
    voice's token is tried without waiting, and a tone that cannot have it
    at once is simply skipped; held for the length of the tone and no more.

    Never for a Quiet conversation — quiet means silent, question or not —
    and never on a phone that has asked for quiet (its ringer verdict), the
    same rule an unasked-for alert follows. Never raises.
    """
    try:
        from .. import earcons, speak_priority

        if not earcons.enabled("held"):
            return False
        if session and speak_priority.level_of(session) == "quiet":
            return False
        from .. import audio_targets
        from ..sinks.speech import SinkSpeech
        from ..state import StateStore
        from ..types import Target
        from .submit import _PRIO_RANK, Priority, _SpeechPlaybackLock

        if StateStore().get_now_playing("speech"):
            return False
        target = Target(name=audio_targets.speech_default())
        lock = _SpeechPlaybackLock(kind="earcon")
        took = lock.take_within(0.0, rank=_PRIO_RANK[Priority.LOW],
                                session="earcon")
        if not took and not lock._disabled():
            return False        # someone is speaking, or about to
        try:
            return earcons.play("held", target, SinkSpeech(), wait=True)
        finally:
            lock.release()
    except Exception as e:  # noqa: BLE001 — a tone must never cost the hold
        log.info("toast: held earcon skipped: %s", e)
        return False


def hold(event: Event) -> None:
    """Render and archive a reply unplayed, and put up the toast."""
    remember(event)
    from .hook_claude_code import _play_detached

    _play_detached(event)


def drop_asks(session: str) -> None:
    """The session's question was answered: its read-out, if still waiting,
    must not play when the conversation is opened, and has been heard."""
    from ..state import StateStore

    for p in _pending():
        try:
            r = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if r.get("ask") and r.get("session") == session:
            try:
                p.unlink()
            except OSError:
                pass
            rid = _row_for(r.get("key") or "")
            if rid is not None:
                StateStore().mark_heard(rid)


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


def take_for_opened(session: str) -> Optional[int]:
    """The conversation `session` was just opened in the app: its newest
    held Normal reply that has never been played, as a history row id to
    replay, or None. All of the session's waiting toasts are taken, so
    opening it again plays nothing twice; older replies keep their Play."""
    from ..speak_priority import level_of
    from ..state import StateStore

    if not session:
        return None
    mine = []
    for p in _pending():
        try:
            r = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if r.get("session") == session:
            mine.append((p, r.get("key") or ""))
    if not mine:
        return None
    for p, _key in mine:
        try:
            p.unlink()
        except OSError:
            pass
    _after_take()
    if level_of(session) != "normal":
        return None
    rid = _wait_for_row(mine[-1][1]) if mine[-1][1] else None
    if rid is None:
        return None
    ex = (StateStore().history_row(rid) or {}).get("extras") or {}
    return None if ex.get("heard") else rid


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
