"""Which conversation is this, and is it still going.

Nothing here is a new noun. Every speech row already carries the two facts
this module needs — `source_session` and `source_pane` in its extras — and
`StateStore.session_for_pane` already exists to answer, in its own words,
"which conversation is this pane". Speech history *is* the thread: the words
went out through it, tagged, in order. So a question asked from a phone does
not need a conversation table, a thread id or a registry. It needs the newest
tagged turn.

**Session first, not pane first.** The pane→session direction is a heuristic
and says so: a pane outlives the conversations that use it — one observed pane
had carried twelve — so "the last conversation to speak here" can be wrong.
The other direction has no such ambiguity. We start from a session, ask where
it last spoke, and then *verify* that the pane is still its, by asking
`session_for_pane` and requiring the answer to come back the same. A pane that
has been recycled fails that check instead of receiving someone else's mail.

**The transcript is the evidence, and knowing the session makes it exact.**
`tmux send-keys Enter` reports that the key reached the pane, which is not the
same as Claude Code accepting it: a still-initialising TUI swallows text and
Enter without trace, and tmux-relay learned that the expensive way — a runner
reporting work underway when none was. The relay has to grep a whole project
directory for its needle because it does not know whose turn it is. We do:
transcripts are named `<session>.jsonl`, so there is one file to look in and a
hit in it is proof rather than inference.

**One line, always.** A literal newline typed into Claude Code submits. So the
context blurb and the question are joined with a dash and sent as a single
line; anything else submits half a question and leaves the rest in the box.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

#: How long after its last turn a conversation still counts as ongoing.
#: Liveness is mostly decided by the pane — it exists, and it is still this
#: session's — but a pane sitting untouched for days is not a conversation you
#: are in the middle of, whatever tmux thinks.
LIVE_S = 1800.0

#: How long to wait for the typed line to show up in the transcript. A session
#: mid-task accepts a prompt at once; a cold one can take several seconds.
LANDED_S = 12.0
_LANDED_POLL_S = 0.4

#: A short question is a fine needle; a long one is not worth carrying around.
_NEEDLE_N = 60


@dataclass(frozen=True)
class Conversation:
    """One conversation, as the speech history knows it."""

    session: str
    window: str = ""
    tmux: str = ""
    pane: str = ""
    at: float = 0.0
    text: str = ""

    @property
    def label(self) -> str:
        """What to call it out loud. The window name if it has one — that is
        what `media history` groups by — and a stub of the session id if not,
        which is what keeps untagged conversations tellable apart."""
        return self.window[:48] or (f"…{self.session[-4:]}" if self.session
                                    else "(untagged)")

    def age(self, now: Optional[float] = None) -> float:
        return max(0.0, (time.time() if now is None else now) - (self.at or 0.0))


@dataclass(frozen=True)
class Liveness:
    """Whether a question can be put to this conversation, and why not."""

    live: bool
    reason: str

    def __bool__(self) -> bool:
        return self.live


def _claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR")
                or (Path.home() / ".claude")).expanduser()


def transcript(session: str) -> Optional[Path]:
    """The session's transcript file, or None.

    Transcripts are named for the session, so this is a lookup and not a
    search. Which project directory holds it does not matter and is not
    guessed at: a session id is unique across all of them.
    """
    session = (session or "").strip()
    if not session or "/" in session or session.startswith("."):
        return None
    root = _claude_dir() / "projects"
    try:
        for path in root.glob(f"*/{session}.jsonl"):
            return path
    except OSError as e:  # noqa: BLE001 — a surface renders what it got
        log.debug("transcript lookup failed: %s", e)
    return None


def pane_alive(pane: str) -> bool:
    """Whether tmux still has this pane. False when tmux is not here at all."""
    pane = (pane or "").strip()
    if not pane or "#{" in pane:
        return False
    try:
        r = subprocess.run(["tmux", "display-message", "-p", "-t", pane,
                            "#{pane_id}"],
                           capture_output=True, text=True, timeout=5,
                           check=False)
    except Exception as e:  # noqa: BLE001
        log.debug("pane check failed: %s", e)
        return False
    return r.returncode == 0 and r.stdout.strip() == pane


def rows_to_conversations(rows) -> list:
    """Clip rows (`cli._clip_rows`) collapsed to one entry per conversation,
    most recently heard first.

    Rows with no session are dropped, and that is not tidying: the newest rows
    in this history are usually cron — org reminders, alert hooks — which speak
    through the same lane and are nobody's conversation. "The newest turn" is
    the wrong anchor for that reason. "The newest turn that belongs to a
    conversation" is the right one.
    """
    out: list = []
    seen: set = set()
    for r in rows or []:
        sid = str((r or {}).get("session") or "").strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append(Conversation(session=sid,
                                window=str(r.get("window") or "").strip(),
                                tmux=str(r.get("tmux") or "").strip(),
                                pane=str(r.get("pane") or "").strip(),
                                at=float(r.get("at") or 0.0),
                                text=str(r.get("text") or "").strip()))
    return out


def _fetch_rows(n: int) -> list:
    try:
        from .cli import _clip_rows

        return _clip_rows(n)
    except Exception as e:  # noqa: BLE001
        log.debug("clip read failed: %s", e)
        return []


def resolve(session: str = "", rows=None, scan: int = 200) -> Optional[Conversation]:
    """The conversation to address: the named one, else the most recent.

    `scan` is deliberately generous. A conversation that has been quiet for a
    while drops out of a short window, and that exact bug has been paid for
    once already in `session_for_pane`'s docstring — a caller scanned the 50
    newest clips globally, the anchor resolved to nothing, and the list
    silently widened from this conversation to every conversation.
    """
    convs = rows_to_conversations(rows if rows is not None else _fetch_rows(scan))
    session = (session or "").strip()
    if session:
        return next((c for c in convs if c.session == session), None)
    return convs[0] if convs else None


def liveness(conv: Optional[Conversation], *, store=None,
             now: Optional[float] = None, live_s: Optional[float] = None) -> Liveness:
    """Whether this conversation is still going, and a sentence saying why not.

    Three conditions, each with its own answer, because "not live" covers three
    different situations and a surface that conflates them tells the user
    nothing they can act on: the pane is gone (the session ended), the pane
    belongs to someone else now (tmux recycled it), or nothing has been said
    for long enough that calling it ongoing would be a fiction.
    """
    if conv is None or not conv.session:
        return Liveness(False, "no conversation has spoken here yet")
    if not conv.pane:
        return Liveness(False, f"{conv.label} was not recorded against a pane")
    if not pane_alive(conv.pane):
        return Liveness(False, f"{conv.label} has closed")
    owner = None
    try:
        if store is None:
            from .state import StateStore

            store = StateStore()
        owner = store.session_for_pane(conv.pane)
    except Exception as e:  # noqa: BLE001
        log.debug("pane owner lookup failed: %s", e)
        return Liveness(False, "the history could not be read")
    if owner != conv.session:
        return Liveness(False, f"{conv.pane} is a different conversation now")
    window = LIVE_S if live_s is None else live_s
    if conv.age(now) > window:
        mins = int(conv.age(now) // 60)
        return Liveness(False, f"{conv.label} has been quiet for {mins} minutes")
    return Liveness(True, f"{conv.label} is listening")


def needle(text: str) -> str:
    """The distinctive part of a typed line, for finding it in a transcript."""
    return " ".join((text or "").split())[:_NEEDLE_N]


def landed(session: str, mark: str, *, timeout: float = LANDED_S) -> bool:
    """Whether the typed line reached the session, per its own transcript.

    The needle is matched against the JSON-escaped form as well as the plain
    one: the transcript is JSONL, so a question containing a quote or a
    backslash is on disk in escaped form and a literal search for it misses.

    The file-exists check is inside the loop on purpose. A session that has
    never had a turn has no transcript yet, so testing once up front would
    report "did not land" for a line that lands a second later — the exact
    false negative this exists to avoid.
    """
    mark = needle(mark)
    if not mark:
        return False
    escaped = json.dumps(mark)[1:-1]
    deadline = time.time() + max(0.0, timeout)
    while True:
        path = transcript(session)
        if path is not None:
            try:
                blob = path.read_text(errors="replace")
                if mark in blob or escaped in blob:
                    return True
            except OSError as e:  # noqa: BLE001
                log.debug("transcript read failed: %s", e)
        if time.time() >= deadline:
            return False
        time.sleep(_LANDED_POLL_S)


def compose(question: str, context: str = "", via: str = "media ask") -> str:
    """The one line to type: where it came from, what was playing, the question.

    The tag is not decoration. A submitted line arrives as a user message and
    is otherwise indistinguishable from David typing it at the keyboard —
    which invites the session to answer as though he were sitting there, when
    in fact he is somewhere else and the reply is going to reach him as speech.
    tmux-relay's watcher tags its nudges for the same reason.
    """
    parts = [p for p in (" ".join((context or "").split()),
                         " ".join((question or "").split())) if p]
    if not parts:
        return ""
    return f"[{via}] " + " — ".join(parts)


def deliver(conv: Conversation, line: str, *,
            verify: bool = True, timeout: float = LANDED_S) -> bool:
    """Type one line into the conversation's pane and submit it.

    Two calls, not one: the text goes in literally (`-l`, so a question
    containing tmux key names is text and not keys), then Enter separately.
    """
    line = " ".join((line or "").split())
    if not line or not conv.pane:
        return False
    try:
        typed = subprocess.run(["tmux", "send-keys", "-t", conv.pane, "-l", line],
                               capture_output=True, timeout=5, check=False)
        if typed.returncode != 0:
            return False
        sent = subprocess.run(["tmux", "send-keys", "-t", conv.pane, "Enter"],
                              capture_output=True, timeout=5, check=False)
        if sent.returncode != 0:
            return False
    except Exception as e:  # noqa: BLE001
        log.debug("delivery failed: %s", e)
        return False
    if not verify:
        return True
    return landed(conv.session, line, timeout=timeout)
