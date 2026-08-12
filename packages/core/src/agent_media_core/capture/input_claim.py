"""Who owns David's next utterance, when the owner is not on this host.

The rendezvous (`rendezvous.py`) answers that question for a *local* asker, and
answers it well: the asker holds a unix socket open for the life of its
question, so `_is_live()` is a real liveness probe and the claim releases when
the process dies. Nothing can leak.

Cece cannot do that. She is Claude live in the Claude Android app — she runs in
Anthropic's cloud, and the phone is only her microphone. There is no process of
hers on red5 to hold a socket, so the one mechanism that makes the local
rendezvous safe has nothing to attach to.

What she does have is an observable proxy: a live session owns the phone mic,
and the phone can see that locally. The Automate mic-detect bridge already
fires on exactly that edge (see `call_guard`'s external hold). So the claim
arrives as a *push from the phone over the tailnet* — p8a is direct-connected
to red5, so it lands in well under a second, against the 5-10s the relay would
cost via `d1-runner`'s poll. This module is the red5-side landing pad.

## Why a freshness window and not a lease

A lease implies negotiation and renewal-on-demand. This is simpler: the phone
re-asserts the claim periodically, and a claim older than its own `ttl_s` is
ignored. That covers the case that actually happens — the phone drops off the
tailnet, or Automate is killed, mid-session — without anyone having to
implement release. There is no deadlock to engineer around because nothing here
can hold the floor by being silent; silence *is* the release.

The window is seconds, not minutes, and it fails OPEN in every direction: an
absent file, an unreadable one, unparseable JSON, a clock that jumped — all
read as "nobody has claimed it". The worst case is sam speaking over Cece,
which is the status quo. The worst case of failing closed is a channel that is
silent forever, which is strictly worse and is the reason `migrations/0008` in
tmux-relay refused to make the D1 mirror load-bearing.

## The path is deliberately fixed

Not overridable, for the reason `call_guard._FLAG_ADVERT_NAME` documents at
length: two parties that resolve a configurable path differently both report
success while writing and reading different files, and that failure is silent.
Barge-in stayed broken for two days in August 2026 exactly that way. The writer
here is an HTTP handler in a *different interpreter* (`speech-state-server.py`
runs on the system python, not this venv, so it cannot import this module) —
which is precisely the situation where a divergent override would go unnoticed.
One fixed location, agreed by both sides, is worth more than the flexibility.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path


log = logging.getLogger(__name__)

# Fixed by contract. `speech-state-server.py` hard-codes the same literal;
# if you change one you must change both.
PATH = Path.home() / ".local" / "state" / "agent-media" / "input-claim.json"

# What a claim is worth if the writer named no window. Long enough to survive a
# missed re-assert, short enough that a vanished phone frees the floor before
# anyone notices. The phone should re-assert at roughly a third of this.
DEFAULT_TTL_S = 45.0


def claim(owner: str, ttl_s: float = DEFAULT_TTL_S, source: str = "") -> None:
    """Record that `owner` holds David's input for the next `ttl_s` seconds.

    Idempotent and cheap: re-asserting is the normal case, once every N seconds
    for as long as the session lasts, so this is a small atomic file write and
    nothing else.
    """
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".json.tmp")
    payload = {
        "owner": owner,
        "source": source,
        "at": time.time(),
        "ttl_s": float(ttl_s),
    }
    try:
        tmp.write_text(json.dumps(payload))
        tmp.replace(PATH)        # atomic: a reader sees whole JSON or the old
    except OSError as e:
        log.warning("input claim: could not write %s: %s", PATH, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def release(owner: str | None = None) -> None:
    """Drop the claim. With `owner`, only if they are the current holder.

    The owner check exists so a late release from a finished session cannot
    free a claim a *newer* session has since taken — the same reason
    `media speech-hold --release` only lifts your own.
    """
    if owner is not None:
        cur = _read()
        if cur is not None and cur.get("owner") != owner:
            return
    try:
        PATH.unlink(missing_ok=True)
    except OSError as e:
        log.warning("input claim: could not clear %s: %s", PATH, e)


def _read() -> dict | None:
    """Whatever is on disk, without applying freshness. None if unusable."""
    try:
        data = json.loads(PATH.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("owner"):
        return None
    return data


def held() -> dict | None:
    """The live claim, or None. Fails open on anything unexpected.

    Returns the stored dict plus `age_s`. A claim whose age exceeds its own
    `ttl_s` is treated as absent — see the freshness note in the module
    docstring. A negative age (the writer's clock ahead of ours) is accepted
    rather than rejected: a clock skew between two hosts on the same tailnet is
    not evidence that nobody is talking.
    """
    data = _read()
    if data is None:
        return None
    try:
        age = time.time() - float(data.get("at", 0))
        ttl = float(data.get("ttl_s", DEFAULT_TTL_S))
    except (TypeError, ValueError):
        return None
    if age > ttl:
        return None
    return {**data, "age_s": age}


def describe() -> str:
    """One line for humans and selfcheck. Never raises."""
    cur = held()
    if cur is None:
        stale = _read()
        if stale is not None:
            return f"input: unclaimed (last {stale.get('owner')}, expired)"
        return "input: unclaimed"
    return (f"input: {cur['owner']} holds it "
            f"({cur['age_s']:.0f}s ago, ttl {cur['ttl_s']:.0f}s"
            + (f", via {cur['source']}" if cur.get("source") else "") + ")")


def enabled() -> bool:
    """Off with MEDIA_INPUT_CLAIM=0 — the escape hatch for a wedged claim.

    `release()` is the normal fix; this exists for the case where something is
    re-asserting a claim you want to ignore and you cannot stop it from here.
    """
    return os.environ.get("MEDIA_INPUT_CLAIM", "1") != "0"
