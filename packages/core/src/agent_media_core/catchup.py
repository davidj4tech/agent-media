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
    "Begin with \"While you were {busy}\". No markdown, no lists, no "
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

def collect(store, since: float, until: float,
            title_of: Optional[Callable[[str], Optional[str]]] = None
            ) -> "list[dict]":
    """The busy spell's held items, oldest first: ``{kind, thread, text,
    id}``. A held reply already heard (played from its Play) is left out."""
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
        if ex.get("held_why") == "busy" and not ex.get("heard"):
            kind = "reply"
        elif ex.get("silenced") == "busy":
            kind = "alert"
        else:
            continue
        session = str(ex.get("session") or ex.get("source_session") or "")
        thread = ""
        if session and title_of is not None:
            try:
                thread = title_of(session) or ""
            except Exception:  # noqa: BLE001 — a name, never a reason to fail
                thread = ""
        if not thread:
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


def _count(n: int, one: str, many: str) -> str:
    return f"{'one' if n == 1 else n} {one if n == 1 else many}"


def template(items: "list[dict]", why) -> str:
    """The catch-up with no model: counts and thread names."""
    replies = [i for i in items if i["kind"] == "reply"]
    alerts = [i for i in items if i["kind"] == "alert"]
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
    return (f"While you were {_busy_phrase(why)}: " + "; ".join(parts) + ". "
            "They're in the app, each with its Play.")


def summarise(items: "list[dict]", why) -> Optional[str]:
    """The gateway's catch-up, or None (the caller uses the template)."""
    from .intake import _summary

    material = "\n\n".join(
        f"[{i['kind']}{' — ' + i['thread'] if i['thread'] else ''}]\n{i['text']}"
        for i in items)
    prompt = PROMPT.format(words=MAX_WORDS, busy=_busy_phrase(why))
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


def speak(items: "list[dict]", why, since: float) -> Optional[int]:
    """Compose and say the catch-up. Returns the history id, or None when
    there was nothing to say."""
    if not items:
        return None
    try:
        from . import jev
        if jev._mode() != "off" and jev._key():
            jev.catchup_worth(items, "speak")
    except Exception:  # noqa: BLE001 — shadow only
        pass
    text, how = compose(items, why)
    from .intake.submit import submit_event
    from .types import Event, Priority, Source

    event = Event(text=text, source=Source.WATCHER, priority=Priority.HIGH,
                  metadata={"kind": "catchup", "catchup_how": how,
                            "catchup_items": [i["id"] for i in items],
                            "busy_since": since, "busy_why": sorted(why)})
    log.info("catchup: %d item(s), %s", len(items), how)
    return submit_event(event)


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
    while True:
        try:
            now = time.time()
            due = w.tick(free.answer(now=now), now)
            if due is not None:
                items = collect(StateStore(), due.since, now, title_of)
                if items:
                    _hand_off(items, due)
                else:
                    log.info("catchup: free again, nothing held")
        except Exception as e:  # noqa: BLE001 — the loop must outlive a bad tick
            log.warning("catchup: tick failed: %s", e)
        time.sleep(TICK_S)


def _hand_off(items: "list[dict]", spell: Spell) -> None:
    payload = json.dumps({"items": items, "why": sorted(spell.why),
                          "since": spell.since})
    p = subprocess.Popen([sys.executable, "-m", "agent_media_core.catchup", "speak"],
                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    p.stdin.write(payload.encode())
    p.stdin.close()
    print(f"catchup: {len(items)} item(s) after {','.join(sorted(spell.why))}",
          file=sys.stderr, flush=True)


def main(argv: "Optional[list[str]]" = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] != ["speak"]:
        print("usage: python -m agent_media_core.catchup speak  < {items, why, since}")
        return 2
    try:
        data = json.loads(sys.stdin.read())
    except ValueError:
        return 2
    speak(list(data.get("items") or []), set(data.get("why") or ()),
          float(data.get("since") or 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
