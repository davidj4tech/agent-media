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

#: How many times `submit` will press Enter before giving up, and how long it
#: waits for the evidence after each press. Three is not superstition: a TUI
#: that is still painting eats the keys it is sent, and the presses have to
#: outlast the painting. An Enter into a composer that has already let go is a
#: no-op, so a spare press costs nothing and a missing one costs the turn.
SUBMIT_PRESSES = 3
_RETRY_LANDED_S = 3.0

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


def session_for_pane(pane: str) -> Optional[str]:
    """Which conversation *currently owns* `pane`, or None.

    Read from the registry `claude-tmux-session-register` (agent-config's
    SessionStart/SessionEnd hook) already maintains at
    ~/.claude/tmux-sessions/<pane-number>, so nothing new has to be written to
    answer this. Format: ``<sessionId> <claudePid> <cwd>``, with a legacy bare
    ``<sessionId>``; keyed by pane, newest start wins.

    Why prefer it over the clip history: history can only answer "who spoke here
    last", and that degrades every time tmux recycles a pane id — one observed
    pane had carried twelve conversations plus fifteen untagged clips, so the
    honest answer from clips can be a conversation that ended days ago. The
    registry knows the live occupant even before it has said anything.

    The pid is what makes a stale entry *detectable* rather than merely old: a
    dead one means the registry is describing a session that has exited, which
    owns nothing. Fall back in that case rather than trust it.

    MEDIA_PANE_REGISTRY_DIR overrides the location (tests, and any host that
    keeps its Claude state elsewhere).
    """
    if not pane or "#{" in pane:
        return None
    root = os.environ.get("MEDIA_PANE_REGISTRY_DIR") or "~/.claude/tmux-sessions"
    path = os.path.join(os.path.expanduser(root), pane.lstrip("%"))
    try:
        with open(path, encoding="utf-8") as fh:
            fields = fh.read().strip().split()
    except OSError:
        # The registry can lose an entry; Claude's own record of the pane
        # (~/.claude/sessions) cannot be stale the same way.
        from . import claude_sessions

        return claude_sessions.by_pane().get("%" + pane.lstrip("%"))
    if not fields:
        return None
    sess = fields[0]
    if len(fields) >= 2 and fields[1].isdigit():
        # Shared with the now_playing orphan guard rather than reimplemented:
        # "is this pid still here" has the same conservative-on-error behaviour
        # in both places, which is the behaviour that matters.
        from .state.store import _pid_alive

        # The pane's owner has exited; whatever is there now is not this session.
        if not _pid_alive(int(fields[1])):
            return None
    return sess or None


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


def _press_enter(pane: str) -> bool:
    """One Enter into `pane`. False if tmux would not take it."""
    try:
        r = subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"],
                           capture_output=True, timeout=5, check=False)
        return r.returncode == 0
    except Exception as e:  # noqa: BLE001
        log.debug("Enter failed: %s", e)
        return False


def submit(pane: str, taken, *, first: float, settle: float = _RETRY_LANDED_S,
           presses: int = SUBMIT_PRESSES) -> bool:
    """Press Enter into `pane` until the line goes in. True if it did.

    `taken(window)` is the caller's evidence, and it is the only thing that
    differs between the two places this is needed: here it is the session's
    own transcript (`landed`), and for a surface watching a pane it is the
    composer letting go of the text. Either way it answers, within `window`
    seconds, whether the line is in.

    An Enter has already been sent by the time this is called — the type and
    the submit are one gesture — so the first thing done is waiting `first`
    for *that* one to show. Only then does this start pressing.

    Pressing again is the whole point. A fire-and-forget Enter has no failure
    to report, and a TUI still painting swallows it: a question typed into a
    fresh window on 2026-09-21 sat unsent for half an hour while the phone
    drew a typing indicator over it. The presses outlast the painting, and
    what comes back says whether they worked, so a caller can tell the
    listener the truth instead of shelving a question nobody will answer.
    """
    if taken(first):
        return True
    for _ in range(max(0, presses)):
        if not _press_enter(pane):
            return False
        if taken(settle):
            return True
    return False


def deliver(conv: Conversation, line: str, *,
            verify: bool = True, timeout: float = LANDED_S) -> bool:
    """Type one line into the conversation's pane and submit it.

    Two calls, not one: the text goes in literally (`-l`, so a question
    containing tmux key names is text and not keys), then Enter separately.

    That Enter is not trusted. `verify` hands the outcome to `submit`, which
    presses again if the transcript does not show the line — see there for
    why one press is not enough. `verify=False` has no evidence to wait on
    and so cannot retry either; it still means "typed, and that is all we
    know".
    """
    line = " ".join((line or "").split())
    if not line or not conv.pane:
        return False
    try:
        typed = subprocess.run(["tmux", "send-keys", "-t", conv.pane, "-l", line],
                               capture_output=True, timeout=5, check=False)
        if typed.returncode != 0:
            return False
    except Exception as e:  # noqa: BLE001
        log.debug("delivery failed: %s", e)
        return False
    if not _press_enter(conv.pane):
        return False
    if not verify:
        return True
    return submit(conv.pane,
                  lambda window: landed(conv.session, line, timeout=window),
                  first=timeout)


# ---- starting one ----------------------------------------------------------
#
# When nothing is listening, the honest options are to refuse or to start
# something. `media open-pi` — the popup's `a` — already starts something: a
# fresh window, seeded with the listening context and the question. What it
# does not do is leave anything behind that a second question could find, and
# that is the whole difference here.
#
# So the window is NAMED for what is being asked about. tmux's window name is
# what the speech hook records as `source_window`, which is what
# `Conversation.label` reads back — so the moment the new session speaks, it
# becomes a conversation this module can resolve like any other, and the next
# ask about the same album lands in the same window rather than opening a
# second one. The name is the only new thing, and it is not a new concept: it
# is the label that was already being read.

#: What launches the answering session. Claude Code by default, and that is
#: load-bearing rather than a preference: the conversation identity this whole
#: module turns on — `source_session`, `source_pane` — is written by the
#: agent-media hook inside a Claude Code session. A launcher that does not
#: write those starts something that can answer once and never be found again.
ASK_CMD = "claude"

#: tmux takes `:` and `.` as target separators, so a window named with one in
#: it cannot be addressed afterwards — which would defeat the point of naming.
_NAME_BAD = ":."
_NAME_N = 40


def window_name(channel: str, title: str = "") -> str:
    """What to call the window: what is being asked about, not who is asking.

    Stable for the same subject, because that is what makes a second question
    land in the first window instead of opening another one beside it.
    """
    subject = " ".join((title or "").split())
    for ch in _NAME_BAD:
        subject = subject.replace(ch, " ")
    subject = " ".join(subject.split())[:_NAME_N].strip()
    return f"ask {subject}" if subject else f"ask {channel or 'speech'}"


def find_window(name: str, session: str = "") -> Optional[str]:
    """The pane of a window by this name, if one is already open.

    The one place the derived name earns its keep. A conversation becomes
    findable through the speech history only once it has *spoken*, so two
    questions asked a minute apart — before the first answer lands — would each
    see nothing listening and each open a window. Looking the name up closes
    that gap.
    """
    name = (name or "").strip()
    if not name:
        return None
    # `-s`, not a bare `-t`. `list-panes -t <session>` lists the panes of that
    # session's CURRENT window only, so without it the lookup found nothing and
    # every question opened another window with the same name — which tmux
    # allows, and which makes the name unaddressable afterwards.
    target = ["-s", "-t", session] if session else ["-a"]
    try:
        r = subprocess.run(["tmux", "list-panes", *target, "-F",
                            "#{window_name}\t#{pane_id}"],
                           capture_output=True, text=True, timeout=5,
                           check=False)
    except Exception as e:  # noqa: BLE001
        log.debug("window lookup failed: %s", e)
        return None
    if r.returncode != 0:
        return None
    for row in r.stdout.splitlines():
        got, _, pane = row.partition("\t")
        if got.strip() == name and pane.strip():
            return pane.strip()
    return None


def start(prompt: str, *, channel: str = "speech", title: str = "",
          session: str = "", cmd: str = "") -> Optional[str]:
    """Open a window named for the subject, running the answerer on `prompt`.

    The prompt goes in as an argument rather than being typed, and that is why
    this path has no transcript check: there is no TUI to swallow it. A process
    that is exec'd with its first message either starts or does not, and tmux
    says which.

    `session` is where to put the window — by default beside the conversation
    that has gone quiet, which is where it belongs. Falling back to tmux's own
    default matters when this is reached over ssh from the phone, where there
    is no attached client to infer one from.
    """
    import shlex

    prompt = " ".join((prompt or "").split())
    if not prompt:
        return None
    name = window_name(channel, title)
    existing = find_window(name, session)
    if existing is not None:
        # Already open and not yet spoken. Type into it rather than opening a
        # second window with the same name, which tmux allows and nobody wants.
        #
        # Not yet spoken is exactly why this used to go in unverified: with no
        # turn in the history there was no session to name, so there was no
        # transcript to check and no way to notice a swallowed Enter — in the
        # one window most likely to swallow one, having just been opened. The
        # registry knows a pane's occupant from the moment it starts, which is
        # earlier than the history can, so ask it. Still unverified when it
        # cannot say: typed is all we would know.
        owner = session_for_pane(existing) or ""
        return name if deliver(Conversation(session=owner, pane=existing), prompt,
                               verify=bool(owner)) else None
    launcher = (cmd or os.environ.get("MEDIA_ASK_CMD") or ASK_CMD).strip()
    argv = ["tmux", "new-window", "-d", "-n", name]
    if session:
        argv += ["-t", session]
    where = os.environ.get("MEDIA_ASK_DIR") or str(Path.home())
    argv += ["-c", where, f"{launcher} {shlex.quote(prompt)}"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10,
                           check=False)
    except Exception as e:  # noqa: BLE001
        log.debug("could not start a conversation: %s", e)
        return None
    if r.returncode != 0:
        log.debug("new-window refused: %s", (r.stderr or "").strip()[:200])
        return None
    return name
