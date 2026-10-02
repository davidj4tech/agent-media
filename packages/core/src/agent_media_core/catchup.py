"""One spoken catch-up when David is free again.

Roadmap item 14, step 3 (docs/proposals/2026-10-01-speaks-when-youre-free.md
§4–§5). While David is busy (free.py) the gate in ``intake.submit`` holds
non-urgent speech: a reply is archived with a Play (``held_why: busy``), an
alert is written down unspoken (``silenced: busy``). When he is free again,
this says what he missed in one short clip, instead of a replay of each.

## When

On the busy → free edge, after a settle: 30 s after a call, 60 s after the
ringer comes off quiet, at once after a meeting or "busy for an hour".
Busy again within the settle, and the spell simply goes on. Only if
something was held: nothing held, nothing said.

The :class:`Watcher` decides; :func:`start` runs it in the canvas server
every ``TICK_S``, reading ``free.answer()``, and hands a due catch-up to
``python -m agent_media_core.catchup speak`` (a child, so rendering and
playing never block the server).

## What is said

Built from the busy spell's rows: held replies not yet heard, grouped by
thread, and alerts recorded unspoken. A gateway summary of about 120 words
(``_summary._chat``, as recaps are written); on any failure a template that
says the counts and the thread names. Never silent once due.

Spoken at HIGH (so the gate lets it through), ``kind: catchup``: it lands
in history and replays like any clip, and each held reply keeps its own
Play.

Jev is asked ``catchup_worth`` in shadow (jev.py); it does not decide yet.

## The morning, and on demand

A digest rendered held (the 08:45 agenda, ``media say --hold``) that lands
in a busy spell joins its catch-up, so a morning off a silent phone is one
clip, not two. "Catch me up" any time (``POST /catchup``, ``media
catchup``) reads what is waiting instead: every held reply and digest not
yet heard and every alert recorded unspoken, since the last catch-up or the
last 12 hours.

## The notification

The server keeps the latest catch-up (:func:`last`) and pokes the session
stream; a phone on ``/sessions/events?catchup=1`` gets a ``catchup`` frame
and posts "While you were busy · 2 replies, 1 alert" (server-contract
§6.22).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)

TICK_S = 15.0
#: How long free must last before the catch-up, by what made him busy.
SETTLE_S = {"call": 30.0, "quiet": 60.0, "meeting": 0.0, "manual": 0.0}
#: Most items read into one catch-up, and of each reply's text.
MAX_ITEMS = 30
TEXT_CAP = 400
MAX_WORDS = 120

PROMPT = (
    "You write a short spoken catch-up for David, who was busy and is now "
    "free. Below is what was held back while he was busy: replies from his "
    "assistant's threads, and alerts. Say it in plain sentences, at most "
    "{words} words, most important first: anything that needs a decision "
    "from him, then failures or things that recovered, then what finished. "
    "Name threads by their title. Group several replies from one thread. "
    "Begin with \"{lead}\". No markdown, no lists, no "
    "greetings, nothing that is not in the material."
)

BUSY_PHRASE = {"call": "on a call", "quiet": "away",
               "meeting": "in a meeting", "manual": "busy"}


# --- when ---------------------------------------------------------------------

@dataclass
class Spell:
    since: float
    why: set = field(default_factory=set)


class Watcher:
    """Turns a stream of ``free.answer()`` readings into "say it now"."""

    def __init__(self) -> None:
        self.spell: Optional[Spell] = None
        self.free_at: Optional[float] = None

    def tick(self, answer: dict, now: float) -> Optional[Spell]:
        """The busy spell to catch David up on, once it is due; else None."""
        if not answer.get("free"):
            why = set(answer.get("why") or ())
            if self.spell is None:
                self.spell = Spell(since=float(answer.get("since") or now), why=why)
            else:
                self.spell.why |= why
            self.free_at = None
            return None
        if self.spell is None:
            return None
        if self.free_at is None:
            self.free_at = now
        settle = max((SETTLE_S.get(w, 0.0) for w in self.spell.why), default=0.0)
        if now - self.free_at < settle:
            return None
        done, self.spell, self.free_at = self.spell, None, None
        return done


# --- what -----------------------------------------------------------------------

#: How far back "catch me up" looks when there was no catch-up since.
WAITING_S = 12 * 3600.0


def _kind(ex: dict, waiting: bool) -> Optional[str]:
    """What this history row is to a catch-up, or None. ``waiting``: any held
    or silenced row (on demand), else only what a busy spell held."""
    if ex.get("held") and not ex.get("heard"):
        if ex.get("digest"):
            return "digest"
        if waiting or ex.get("held_why") == "busy":
            return "reply"
        return None
    if ex.get("silenced") and (waiting or ex.get("silenced") == "busy"):
        return "alert"
    return None


def collect(store, since: float, until: float,
            title_of: Optional[Callable[[str], Optional[str]]] = None,
            waiting: bool = False) -> "list[dict]":
    """The held items, oldest first: ``{id, at, kind, thread, text}``, kind
    reply | alert | digest. A reply or digest already heard (played from its
    Play) is left out."""
    items = []
    for r in store.recent_history(sink="speech", limit=500):
        ex = r.get("extras")
        if not isinstance(ex, dict):
            continue
        try:
            at = float(r.get("started_at") or 0)
        except (TypeError, ValueError):
            continue
        if not since <= at <= until:
            continue
        kind = _kind(ex, waiting)
        if kind is None:
            continue
        session = str(ex.get("session") or ex.get("source_session") or "")
        thread = ""
        if session and title_of is not None:
            try:
                thread = title_of(session) or ""
            except Exception:  # noqa: BLE001 — a name, never a reason to fail
                thread = ""
        if not thread and kind == "reply":
            thread = str(ex.get("source_tmux_session") or "")
        items.append({"id": r.get("id"), "at": at, "kind": kind,
                      "thread": thread,
                      "text": str(r.get("text") or "").strip()[:TEXT_CAP]})
    items.sort(key=lambda i: i["at"])
    return items[-MAX_ITEMS:]


def _busy_phrase(why) -> str:
    for w in ("meeting", "call", "manual", "quiet"):
        if w in why:
            return BUSY_PHRASE[w]
    return "busy"


def _lead(why) -> str:
    """How it begins: the busy spell, or (on demand, no spell) what waits."""
    return f"While you were {_busy_phrase(why)}" if why else "Here's what's waiting"


def _count(n: int, one: str, many: str) -> str:
    return f"{'one' if n == 1 else n} {one if n == 1 else many}"


def counts(items: "list[dict]") -> dict:
    """``{replies, alerts, digests}``: the notification's line."""
    return {"replies": sum(1 for i in items if i["kind"] == "reply"),
            "alerts": sum(1 for i in items if i["kind"] == "alert"),
            "digests": sum(1 for i in items if i["kind"] == "digest")}


def template(items: "list[dict]", why) -> str:
    """The catch-up with no model: counts and thread names."""
    replies = [i for i in items if i["kind"] == "reply"]
    alerts = [i for i in items if i["kind"] == "alert"]
    digests = [i for i in items if i["kind"] == "digest"]
    parts = []
    if replies:
        threads = []
        for i in replies:
            if i["thread"] and i["thread"] not in threads:
                threads.append(i["thread"])
        s = _count(len(replies), "reply", "replies")
        if threads:
            named = threads[:4]
            s += " from " + (", ".join(named[:-1]) + " and " + named[-1]
                             if len(named) > 1 else named[0])
            if len(threads) > 4:
                s += f" and {len(threads) - 4} more"
        parts.append(s)
    if alerts:
        first = (alerts[0]["text"].splitlines()[0][:80].rstrip(". ")
                 if alerts[0]["text"] else "")
        s = _count(len(alerts), "alert", "alerts")
        if first:
            s += f", the first: {first}"
        parts.append(s)
    if digests:
        parts.append(_count(len(digests), "digest", "digests") + " to play")
    return (f"{_lead(why)}: " + "; ".join(parts) + ". "
            "They're in the app, each with its Play.")


def summarise(items: "list[dict]", why) -> Optional[str]:
    """The gateway's catch-up, or None (the caller uses the template)."""
    from .intake import _summary

    material = "\n\n".join(
        f"[{i['kind']}{' — ' + i['thread'] if i['thread'] else ''}]\n{i['text']}"
        for i in items)
    prompt = PROMPT.format(words=MAX_WORDS, lead=_lead(why))
    try:
        timeout = int(os.environ.get("MEDIA_CATCHUP_TIMEOUT_S") or 30)
    except ValueError:
        timeout = 30
    # The recaps' model, not the speech summary's: that one is local and slow.
    model = (os.environ.get("MEDIA_CATCHUP_MODEL")
             or os.environ.get("MEDIA_FOLLOWUP_MODEL") or None)
    out = _summary._chat(prompt, material, timeout, model=model)
    if not out:
        return None
    words = out.split()
    if len(words) > MAX_WORDS * 1.5:
        out = " ".join(words[:int(MAX_WORDS * 1.5)])
    return out


def compose(items: "list[dict]", why) -> "tuple[str, str]":
    """``(text, how)``: the summary when the gateway gives one, else the
    template. ``how`` is "summary" or "template"."""
    if os.environ.get("MEDIA_CATCHUP_SUMMARY", "1") != "0":
        out = summarise(items, why)
        if out:
            return out, "summary"
    return template(items, why), "template"


# --- the latest, for the frame ----------------------------------------------------

_LOCK = threading.Lock()
_LAST: Optional[dict] = None
_SEQ = 0
_listeners: "list[Callable[[], None]]" = []


def last() -> Optional[dict]:
    """The latest catch-up made here: ``{id, at, text, how, why, replies,
    alerts, digests}``, or None."""
    with _LOCK:
        return dict(_LAST) if _LAST else None


def on_made(fn: "Callable[[], None]") -> None:
    """Call ``fn`` after each catch-up is made (the stream's poke)."""
    _listeners.append(fn)


def _tell() -> None:
    for fn in list(_listeners):
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass


def deliver(items: "list[dict]", why, since: float) -> Optional[dict]:
    """Compose the catch-up, record it, tell the listeners, and have it
    said. Returns the record, or None when there was nothing to say."""
    global _LAST, _SEQ
    if not items:
        return None
    try:
        from . import jev
        if jev._mode() != "off" and jev._key():
            jev.catchup_worth(items, "speak")
    except Exception:  # noqa: BLE001 — shadow only
        pass
    text, how = compose(items, why)
    with _LOCK:
        _SEQ += 1
        _LAST = {"id": f"{int(time.time())}-{_SEQ}", "at": round(time.time(), 3),
                 "text": text, "how": how, "why": sorted(why), **counts(items)}
        rec = dict(_LAST)
    _tell()
    _say({"text": text, "how": how, "items": [i["id"] for i in items],
          "why": sorted(why), "since": since})
    print(f"catchup: {len(items)} item(s), {how}"
          + (f" after {','.join(sorted(why))}" if why else " on demand"),
          file=sys.stderr, flush=True)
    return rec


def _say(payload: dict) -> None:
    """Speak it from a child: rendering and playing never hold the server."""
    p = subprocess.Popen([sys.executable, "-m", "agent_media_core.catchup", "say"],
                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    p.stdin.write(json.dumps(payload).encode())
    p.stdin.close()


def say(payload: dict) -> Optional[int]:
    """The child's half: one HIGH clip, kind catchup."""
    text = str(payload.get("text") or "").strip()
    if not text:
        return None
    from .intake.submit import submit_event
    from .types import Event, Priority, Source

    return submit_event(Event(
        text=text, source=Source.WATCHER, priority=Priority.HIGH,
        metadata={"kind": "catchup", "catchup_how": payload.get("how"),
                  "catchup_items": list(payload.get("items") or []),
                  "busy_since": payload.get("since"),
                  "busy_why": list(payload.get("why") or [])}))


def request(title_of: Optional[Callable[[str], Optional[str]]] = None,
            store=None, now: Optional[float] = None) -> int:
    """"Catch me up", on demand: what is waiting since the last catch-up or
    the last 12 hours. Composed and said in the background; returns how many
    items it covers (0: nothing is said)."""
    from .state import StateStore

    now = time.time() if now is None else now
    prev = last()
    since = max(now - WAITING_S, float(prev["at"]) if prev else 0.0)
    items = collect(store or StateStore(), since, now, title_of, waiting=True)
    if items:
        threading.Thread(target=deliver, args=(items, set(), since),
                         daemon=True, name="catchup-now").start()
    return len(items)


# --- the loop, in the canvas server -------------------------------------------

_started = False


def start(title_of: Optional[Callable[[str], Optional[str]]] = None) -> None:
    """Run the watcher in a daemon thread. ``MEDIA_CATCHUP=0`` leaves it off."""
    global _started
    if _started or os.environ.get("MEDIA_CATCHUP", "1") == "0":
        return
    _started = True
    threading.Thread(target=_loop, args=(title_of,), daemon=True,
                     name="catchup").start()


def _loop(title_of) -> None:
    from . import free
    from .state import StateStore

    w = Watcher()
    seen_state = None
    while True:
        try:
            now = time.time()
            # A reply held since the last tick changes the `free` frame's
            # count, and nothing else would wake the stream for it.
            st = free.state(now=now)
            if seen_state is not None and st != seen_state:
                _tell()
            seen_state = st
            due = w.tick(free.answer(now=now), now)
            if due is not None:
                items = collect(StateStore(), due.since, now, title_of)
                if items:
                    deliver(items, due.why, due.since)
                else:
                    log.info("catchup: free again, nothing held")
        except Exception as e:  # noqa: BLE001 — the loop must outlive a bad tick
            log.warning("catchup: tick failed: %s", e)
        time.sleep(TICK_S)


def main(argv: "Optional[list[str]]" = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["say"]:
        try:
            data = json.loads(sys.stdin.read())
        except ValueError:
            return 2
        say(data if isinstance(data, dict) else {})
        return 0
    print("usage: python -m agent_media_core.catchup say  < {text, how, items, why, since}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
