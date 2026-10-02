"""Ask Jev the soft half of "can David be spoken to now?".

## What this is for

Roadmap item 14 (docs/proposals/2026-10-01-speaks-when-youre-free.md) holds
non-urgent speech while David is busy and says one catch-up when he is free.
The hard facts are rules: a call, a quiet ringer, "busy for an hour". Three
questions are not facts, and a rule answers them badly:

- **event_busy**: is this calendar event really "don't talk to me"? "Focus
  time", "Gym" and "Dentist" are all marked busy.
- **breaks_through**: should this held item speak anyway? "prod is down" at
  normal level should; an alert about a disk at 81 % should not.
- **catchup_worth**: is the catch-up worth a voice, or only a card?

Jev (TypeSafe's typed-decision model, https://docs.typesafe.ai/api.md)
answers each as a choice or a yes/no with a probability, in well under a
second, for fractions of a cent a day at this volume.

## The rule always has the last word it can

Every function takes the rule's answer from the caller and returns it
unless Jev is switched on, answered in time, and is at least
``MEDIA_JEV_THRESHOLD`` sure (0.7). No key, a timeout, an error, an unsure
answer: the rule. Jev can move a decision, never block one, so the gate
item 14 builds stays fail-open.

``MEDIA_JEV_MODE``: ``off`` (never ask), ``shadow`` (ask and log, the rule
decides; the default), ``on`` (Jev decides when sure).

## The log is the point

Every question asked is a line in ``jev-decisions.jsonl``: what was asked,
Jev's answer and probability, what the rule said, which one was used. That
is how to find out whether Jev is right before letting it decide, and how
to tune the threshold. ``python -m agent_media_core.jev trial`` asks the
three questions on real cases and prints Jev beside the rule.

## What leaves the host

An event's title, location, attendee count, status and length; a held
item's kind, level, thread title and first 300 characters. Nothing else.
"""

from __future__ import annotations

import json
import os
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from ._paths import state_dir

API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT_S = 1.5
DEFAULT_THRESHOLD = 0.7
TEXT_CAP = 300

EVENT_CHOICES = {
    "hold": "Speech in the ear would be unwelcome: a meeting, a call, an "
            "appointment, a class, or time blocked to concentrate.",
    "light": "Only something important should interrupt: a meal, the gym, "
             "travel, an errand.",
    "free": "Not really busy: a reminder, a placeholder, a holiday, someone "
            "else's event, something tentative or already over.",
}
CATCHUP_CHOICES = {
    "speak": "Worth saying aloud: a decision is wanted, something failed or "
             "recovered, or several threads moved.",
    "card": "Not worth interrupting for: routine completions, one short "
            "acknowledgement, nothing that needs David.",
}


@dataclass
class Decision:
    """What a question decided, and why."""
    answer: Any
    source: str               # "jev" or "rule"
    p: Optional[float] = None  # Jev's probability for its answer, if asked
    jev: Any = None            # Jev's answer, if it gave one
    why: str = ""              # off | nokey | error | timeout | shadow | unsure | sure


def log_path() -> Path:
    return state_dir() / "jev-decisions.jsonl"


def _mode() -> str:
    m = (os.environ.get("MEDIA_JEV_MODE") or "shadow").strip().lower()
    return m if m in ("off", "shadow", "on") else "shadow"


def _threshold() -> float:
    try:
        return float(os.environ.get("MEDIA_JEV_THRESHOLD") or DEFAULT_THRESHOLD)
    except ValueError:
        return DEFAULT_THRESHOLD


def _key() -> str:
    return (os.environ.get("TYPESAFE_API_KEY") or "").strip()


def _cap(text: Any) -> str:
    return str(text or "").strip()[:TEXT_CAP]


def ask(state: Any, questions: dict, timeout: Optional[float] = None
        ) -> "tuple[Optional[dict], str, int]":
    """POST one System One request. Returns (answers, why, ms): answers is
    None on no key, timeout or any error, with ``why`` saying which. Never
    raises."""
    key = _key()
    if not key:
        return None, "nokey", 0
    body = json.dumps({
        "state": state,
        "model": os.environ.get("MEDIA_JEV_MODEL") or DEFAULT_MODEL,
        "questions": questions,
    }).encode("utf-8")
    req = urllib.request.Request(
        os.environ.get("MEDIA_JEV_URL") or API_URL, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    if timeout is None:
        try:
            timeout = float(os.environ.get("MEDIA_JEV_TIMEOUT_S")
                            or DEFAULT_TIMEOUT_S)
        except ValueError:
            timeout = DEFAULT_TIMEOUT_S
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except TimeoutError:
        return None, "timeout", int((time.monotonic() - t0) * 1000)
    except urllib.error.URLError as e:
        why = "timeout" if isinstance(e.reason, TimeoutError) else "error"
        return None, why, int((time.monotonic() - t0) * 1000)
    except (OSError, ValueError):
        return None, "error", int((time.monotonic() - t0) * 1000)
    ms = int((time.monotonic() - t0) * 1000)
    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict):
        return None, "error", ms
    return answers, "ok", ms


def _read_choice(ans: Any, options: "list[str]") -> "tuple[Any, Optional[float]]":
    if not isinstance(ans, dict) or ans.get("choice") not in options:
        return None, None
    choice = ans["choice"]
    probs = ans.get("probabilities")
    p = probs.get(choice) if isinstance(probs, dict) else None
    if not isinstance(p, (int, float)):
        p = ans.get("confidence")
    return choice, (float(p) if isinstance(p, (int, float)) else None)


def _read_noul(ans: Any) -> "tuple[Any, Optional[float]]":
    if not isinstance(ans, dict) or not isinstance(ans.get("noul"), (int, float)):
        return None, None
    v = float(ans["noul"])
    return v >= 0.5, max(v, 1.0 - v)


def _decide(question: str, state: Any, q: dict, rule: Any,
            read) -> Decision:
    mode = _mode()
    if mode == "off":
        return Decision(answer=rule, source="rule", why="off")
    answers, status, ms = ask(state, {question: q})
    jev = p = None
    if answers is not None:
        jev, p = read(answers.get(question))
        if jev is None:
            status = "error"
    if jev is None:
        d = Decision(answer=rule, source="rule", why=status)
    elif mode == "shadow":
        d = Decision(answer=rule, source="rule", p=p, jev=jev, why="shadow")
    elif p is None or p < _threshold():
        d = Decision(answer=rule, source="rule", p=p, jev=jev, why="unsure")
    else:
        d = Decision(answer=jev, source="jev", p=p, jev=jev, why="sure")
    _log(question, state, rule, d, ms)
    return d


def _log(question: str, state: Any, rule: Any, d: Decision, ms: int) -> None:
    line = {"ts": time.time(), "question": question, "state": state,
            "rule": rule, "ms": ms, **asdict(d)}
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass


# --- the three questions ----------------------------------------------------

_event_cache: "dict[str, Decision]" = {}


def event_busy(event: dict, rule: str) -> Decision:
    """hold | light | free for a calendar event under way. ``event`` carries
    title, location, attendees (a count), status (accepted/tentative/...),
    minutes (its length) and, for the cache, id. ``rule`` is item 14's
    answer ("hold" for any busy, timed, not declined event)."""
    eid = str(event.get("id") or "")
    if eid and eid in _event_cache:
        return _event_cache[eid]
    state = {"event": {
        "title": _cap(event.get("title")),
        "location": _cap(event.get("location")),
        "attendees": int(event.get("attendees") or 0),
        "status": str(event.get("status") or ""),
        "minutes": int(event.get("minutes") or 0),
    }}
    q = {"type": "choice",
         "instructions": "David has this calendar event under way right now. "
                         "Should an assistant speak non-urgent news into his "
                         "earphones during it?",
         "criteria": EVENT_CHOICES}
    d = _decide("event_busy", state, q, rule,
                lambda a: _read_choice(a, list(EVENT_CHOICES)))
    if eid and d.why not in ("nokey", "error", "timeout"):
        _event_cache[eid] = d
    return d


def breaks_through(item: dict, rule: bool) -> Decision:
    """Should this held item be spoken now, though David is busy? ``item``
    carries kind (reply/alert/digest/alarm), level, thread (its title) and
    text. ``rule`` is item 14's answer (urgent or needs)."""
    state = {"busy_because": str(item.get("busy_because") or ""),
             "item": {"kind": str(item.get("kind") or ""),
                      "level": str(item.get("level") or ""),
                      "thread": _cap(item.get("thread")),
                      "text": _cap(item.get("text"))}}
    q = {"type": "noul",
         "instructions": "David is busy. Is this important enough to speak "
                         "to him now rather than in a catch-up when he is "
                         "free?",
         "criteria": {"true": "Something is broken, at risk or needs him "
                              "within minutes.",
                      "false": "It can wait until he is free."}}
    return _decide("breaks_through", state, q, bool(rule), _read_noul)


def catchup_worth(items: "list[dict]", rule: str) -> Decision:
    """speak | card for what was held while David was busy. Each item as for
    :func:`breaks_through`. ``rule`` is item 14's answer ("speak" whenever
    anything is held)."""
    state = {"held": [{"kind": str(i.get("kind") or ""),
                       "thread": _cap(i.get("thread")),
                       "text": _cap(i.get("text"))} for i in items[:20]],
             "count": len(items)}
    q = {"type": "choice",
         "instructions": "David has just become free. Is what was held while "
                         "he was busy worth a short spoken catch-up, or only "
                         "a notification card?",
         "criteria": CATCHUP_CHOICES}
    return _decide("catchup_worth", state, q, rule,
                   lambda a: _read_choice(a, list(CATCHUP_CHOICES)))


# --- doctor -----------------------------------------------------------------

def facts(now: Optional[float] = None) -> "dict[str, str]":
    """`media doctor` lines: jev=off|nokey|ok|down, the mode, the median
    latency and how often Jev and the rule disagreed in the last 24 h.
    Nothing at all on a host with no key and no questions asked today."""
    now = time.time() if now is None else now
    mode = _mode()
    rows = []
    try:
        with log_path().open(encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and float(r.get("ts") or 0) >= now - 86400:
                    rows.append(r)
    except OSError:
        pass
    if not _key() and not rows:
        return {}  # never set up here: say nothing
    if mode == "off":
        state = "off"
    elif not _key():
        state = "nokey"
    elif rows and rows[-1].get("why") in ("error", "timeout"):
        state = "down"
    else:
        state = "ok"
    out = {"jev": state, "jev_mode": mode}
    answered = [r for r in rows if r.get("jev") is not None]
    if answered:
        out["jev_asked_24h"] = str(len(answered))
        out["jev_p50_ms"] = str(int(statistics.median(
            int(r.get("ms") or 0) for r in answered)))
        out["jev_disagree_24h"] = str(sum(
            1 for r in answered if r.get("jev") != r.get("rule")))
        used = sum(1 for r in answered if r.get("source") == "jev"
                   and r.get("jev") != r.get("rule"))
        if used:
            out["jev_overrides_24h"] = str(used)
    return out


# --- trial (proposal step 1) ------------------------------------------------

SAMPLE_EVENTS = [
    {"title": "Focus time", "minutes": 120},
    {"title": "Weekly 1:1", "attendees": 2, "minutes": 30},
    {"title": "Gym", "minutes": 60},
    {"title": "Dentist", "location": "Collins St", "minutes": 45},
    {"title": "Lunch", "minutes": 60},
    {"title": "Team standup", "attendees": 6, "minutes": 15},
    {"title": "Maybe: drinks", "status": "tentative", "minutes": 120},
    {"title": "Flight to Sydney", "minutes": 95},
    {"title": "Bin night", "minutes": 15},
    {"title": "Yoga class", "location": "Fitzroy", "minutes": 75},
]


def _held_items(limit: int) -> "list[dict]":
    from .state import StateStore
    out = []
    for row in StateStore().recent_history(sink="speech", limit=600):
        ex = row.get("extras")
        if not isinstance(ex, dict) or not ex.get("held"):
            continue
        out.append({"kind": "reply", "level": str(ex.get("priority") or ""),
                    "thread": str(ex.get("title") or ex.get("session") or ""),
                    "text": row.get("text") or ""})
        if len(out) >= limit:
            break
    return out


def trial(n: int = 10) -> int:
    """Ask the three questions on real cases and print Jev beside the rule.
    Logs as usual (shadow), so the lines feed the same review."""
    os.environ["MEDIA_JEV_MODE"] = "shadow"
    if not _key():
        print("TYPESAFE_API_KEY is not set; nothing to ask.")
        return 2
    print("event_busy (rule: hold)")
    for ev in SAMPLE_EVENTS[:n]:
        d = event_busy(ev, "hold")
        print(f"  {d.jev!s:6} p={d.p or 0:.2f}  {ev['title']}")
    items = _held_items(n)
    print(f"\nbreaks_through (rule: no) on {len(items)} held replies")
    for it in items:
        d = breaks_through({**it, "busy_because": "meeting"}, False)
        first = it["text"].splitlines()[0][:70] if it["text"] else ""
        print(f"  {d.jev!s:6} p={d.p or 0:.2f}  {first}")
    if items:
        d = catchup_worth(items, "speak")
        print(f"\ncatchup_worth (rule: speak): {d.jev} p={d.p or 0:.2f}")
    print(f"\nlogged to {log_path()}")
    return 0


def main(argv: "Optional[list[str]]" = None) -> int:
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["trial"]:
        return trial(int(args[1]) if len(args) > 1 else 10)
    if args[:1] == ["facts"]:
        for k, v in facts().items():
            print(f"{k}={v}")
        return 0
    print("usage: python -m agent_media_core.jev trial [N] | facts")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
