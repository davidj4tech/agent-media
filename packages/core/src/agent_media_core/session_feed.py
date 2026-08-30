"""A conversation, as one episode.

The speech channel already writes everything this needs. A finished turn
records its text, every clip it rendered, each clip's sentence and each
clip's duration (`intake/submit.py`, the history extras). So an episode is not
a synthesis job — it is a concatenation, and the chapter marks come from the
turn boundaries that are already in the data.

    media feed session            the conversation this pane is holding
    media feed session <id>       any conversation, by Claude session id

## Why one episode per conversation

A turn is thirty seconds. Nobody subscribes to that, and a client showing four
hundred of them is worse than useless. The conversation is the unit someone
actually wants back — and `extras.source_session` is the only honest boundary
for it: a tmux session holds several conversations, and one conversation moves
panes when it resumes.

Each turn becomes a chapter titled with its first sentence, so the thing a
client's chapter list gives you is a table of contents for the conversation:
skip to the bit where you asked about the ringer.

## Stream copy, verified

The clips are already mp3 at one engine's settings, so `-c copy` is seconds of
work rather than minutes of re-encoding. But a session that fell back to
another engine mid-conversation (`extras.fallback`) can hold clips at two
sample rates, and concatenating *those* by copy produces a file whose header
lies about its own length — it plays, and every chapter mark after the join
points at the wrong moment.

So the result is measured against the sum of the parts, and re-encoded only
when they disagree. The common case stays fast; the broken case does not ship.

## Publishing is re-publishing

The guid is the session id, so a conversation still going can be published now
and again later: the second run replaces the first with a longer episode. That
is the intended way to use it — there is no need to wait for a conversation to
end, and no way to end up with three overlapping copies of one afternoon.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import feed as feedmod
from .docs import _ffprobe_duration, _write_chapter_metadata

log = logging.getLogger(__name__)

#: How far back to look for a conversation's turns. Speech history is shared
#: by every conversation on the host, so scoping to one means over-fetching:
#: a busy day interleaves hundreds of other clips between this session's.
_FETCH = 4000

#: Chapters listed in the episode description. A long conversation's whole
#: table of contents in the XML would be re-fetched on every client poll.
_NOTES_CHAPTERS = 40


@dataclass
class Turn:
    at: float
    text: str
    clips: list = field(default_factory=list)        # list[Path]
    durations: list = field(default_factory=list)    # list[float]

    @property
    def title(self) -> str:
        """The turn's first sentence, as a chapter name."""
        first = " ".join((self.text or "").split())
        for stop in (". ", "? ", "! "):
            i = first.find(stop)
            if 0 < i < 90:
                return first[:i + 1].strip()
        return (first[:87] + "…") if len(first) > 90 else first or "…"


def turns(session: str, *, store=None) -> list[Turn]:
    """This conversation's spoken turns, oldest first.

    Three kinds of row are left out, and none of them is a judgement call:

    - **alerts** (`extras.kind == "notif"`) — "Claude is waiting" is not part
      of the conversation, and the popup's own traversal excludes them for the
      same reason;
    - **rows with no audio** — a `silenced:` row records that something was
      *not* said aloud, and there is nothing to concatenate;
    - **clips the cache has swept** — the row outlives the file (that is the
      whole reason this feature exists), so every path is checked and a turn
      whose audio is entirely gone is dropped rather than faked.

    A turn that was rendered but never heard — muted pane, flushed by a
    barge-in — *is* included. The words were written and spoken by the
    renderer; whether the room was listening is not what the archive is about.
    """
    from .state.store import StateStore

    st = store or StateStore()
    out: list[Turn] = []
    for row in st.recent_history(sink="speech", limit=_FETCH):
        ex = row.get("extras")
        if not isinstance(ex, dict) or ex.get("source_session") != session:
            continue
        if ex.get("kind") == "notif":
            continue
        uris = ex.get("clip_uris") or ([row["uri"]] if row.get("uri") else [])
        durs = list(ex.get("clip_durations_s") or [])
        clips, kept = [], []
        for i, u in enumerate(uris):
            p = Path(str(u))
            if not p.is_file():
                continue
            clips.append(p)
            kept.append(durs[i] if i < len(durs) else _ffprobe_duration(p))
        if not clips:
            continue
        out.append(Turn(at=float(row.get("started_at") or 0),
                        text=(row.get("text") or ""),
                        clips=clips, durations=kept))
    out.sort(key=lambda t: t.at)
    return out


def title_for(session: str, ts: list[Turn]) -> str:
    """What to call the episode.

    The first thing the person asked, which is what they will remember about
    the conversation — the reply's opening sentence is a worse name for it, and
    a session id is no name at all. Falls back through both.

    Read straight from the transcript rather than from any index: the file is
    named for the session, so there is one place to look and a hit in it is
    proof (`conversation.transcript`).
    """
    from .conversation import transcript

    path = transcript(session)
    if path is not None:
        try:
            with path.open(errors="replace") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("type") != "user":
                        continue
                    content = (d.get("message") or {}).get("content")
                    if isinstance(content, list):      # blocks, not a string
                        content = next((b.get("text") for b in content
                                        if isinstance(b, dict)
                                        and b.get("type") == "text"), None)
                    if not isinstance(content, str):
                        continue
                    text = " ".join(content.split())
                    # Tool results, hook injections and system reminders are
                    # user-role messages too, and none of them is a question
                    # anybody asked.
                    if not text or text.startswith(("<", "Caveat:", "[media ")):
                        continue
                    return text[:87] + "…" if len(text) > 90 else text
        except OSError as e:
            log.debug("transcript unreadable for %s: %s", session, e)
    if ts:
        return ts[0].title
    return f"Conversation {session[:8]}"


def notes(ts: list[Turn], limit: int = _NOTES_CHAPTERS) -> str:
    """The episode description: a timestamped table of contents.

    A client shows one text field, and the useful thing to put in it for a
    conversation is where in the hour each part is — the same information as
    the chapter marks, for the clients that don't read them.
    """
    lines, clock = [], 0.0
    for i, t in enumerate(ts):
        if i < limit:
            lines.append(f"{feedmod.hms(clock)}  {t.title}")
        clock += sum(t.durations)
    if len(ts) > limit:
        lines.append(f"… and {len(ts) - limit} more")
    return "\n".join(lines)


def build(ts: list[Turn], out: Path) -> Optional[Path]:
    """Concatenate the turns into `out`, one chapter each. None if it can't.

    The source clips are the render cache's, and are left exactly where they
    are: other things replay them, and this is a copy of the conversation, not
    a move of it.
    """
    if not ts:
        return None
    chapters, clock = [], 0.0
    for t in ts:
        dur = sum(t.durations) or sum(_ffprobe_duration(c) for c in t.clips)
        chapters.append((t.title, clock, clock + dur))
        clock += dur
    expected = clock

    with tempfile.TemporaryDirectory(prefix="media-episode-") as tmp:
        work = Path(tmp)
        listing = work / "parts.txt"
        listing.write_text("".join(f"file '{c.as_posix()}'\n"
                                   for t in ts for c in t.clips))
        meta = work / "chapters.txt"
        _write_chapter_metadata(meta, chapters)

        def _run(codec: list[str]) -> bool:
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error",
                     "-f", "concat", "-safe", "0", "-i", str(listing),
                     "-i", str(meta), "-map_metadata", "1", *codec, str(out)],
                    check=True, capture_output=True, timeout=1800)
            except (OSError, subprocess.SubprocessError) as e:
                log.warning("episode: ffmpeg failed (%s)", e)
                return False
            return out.exists() and out.stat().st_size > 0

        if not _run(["-c", "copy"]):
            return None
        # Mixed sample rates concatenate into a file that plays and lies about
        # its length; every chapter after the join then points at the wrong
        # moment. Measuring is the only way to tell, and it is one ffprobe.
        got = _ffprobe_duration(out)
        if expected > 0 and abs(got - expected) > max(2.0, expected * 0.02):
            log.info("episode: %s ≠ %s expected — re-encoding", got, expected)
            if not _run(["-c:a", "libmp3lame", "-q:a", "4"]):
                return None
    return out


def conversations(store=None) -> list[dict]:
    """Every conversation speech history knows about, newest last turn first.

    One pass over the same rows `turns` reads, grouped — the listing and the
    auto-publisher both want "what is there", and neither wants it per-session
    (which would be one full scan per conversation).
    """
    from .state.store import StateStore

    st = store or StateStore()
    seen: dict = {}
    for row in st.recent_history(sink="speech", limit=_FETCH):
        ex = row.get("extras")
        if not isinstance(ex, dict) or ex.get("kind") == "notif":
            continue
        sess = ex.get("source_session")
        if not sess:
            continue
        at = float(row.get("started_at") or 0)
        cur = seen.setdefault(sess, {"session": sess, "turns": 0,
                                     "first": at, "last": at})
        cur["turns"] += 1
        cur["first"] = min(cur["first"], at)
        cur["last"] = max(cur["last"], at)
    return sorted(seen.values(), key=lambda c: -c["last"])


def publish_quiet(*, name: str = "talks", quiet_s: float = 3600.0,
                  now: Optional[float] = None, store=None,
                  limit: int = 0) -> list[feedmod.Episode]:
    """Publish every conversation that has finished and isn't on the feed yet.

    "Finished" is silence: no turn for `quiet_s`. There is no event for a
    conversation ending — a session id stays valid, a pane stays open, and
    people come back to yesterday's — so quiet is the only signal, and an hour
    of it is a long time in a conversation.

    A session already published is republished only if it has *grown* since.
    That keeps the spool honest about the conversation that actually happened;
    subscribers that already downloaded the shorter episode keep it, because
    every client matches on guid and none re-fetches. The alternative — never
    republishing — loses the tail of any conversation that revives, which is
    worse and silent.
    """
    now = time.time() if now is None else now
    out: list[feedmod.Episode] = []
    published = {e.guid: e for e in feedmod.episodes(name)}
    for conv in conversations(store=store):
        if now - conv["last"] < quiet_s:
            continue                       # still going, or paused mid-thought
        have = published.get(f"session:{conv['session']}")
        if have is not None and have.published >= conv["last"]:
            continue                       # already on the feed, unchanged
        ep = publish(conv["session"], name=name, store=store)
        if ep is None:
            continue                       # no turns whose audio survives
        log.info("published %s (%s)", ep.title, conv["session"])
        out.append(ep)
        if limit and len(out) >= limit:
            break
    return out


def publish(session: str, *, name: str = "talks",
            store=None) -> Optional[feedmod.Episode]:
    """Build this conversation and put it on the feed. None if there is none.

    Published at the conversation's *last* turn, not now: that is when the
    episode became what it is, and it puts an afternoon you are archiving
    tonight where you would look for it rather than at the top of the list.
    """
    ts = turns(session, store=store)
    if not ts:
        return None
    with tempfile.TemporaryDirectory(prefix="media-episode-") as tmp:
        built = build(ts, Path(tmp) / f"{session}.mp3")
        if built is None:
            return None
        return feedmod.publish(
            name, built, guid=f"session:{session}",
            title=title_for(session, ts), description=notes(ts),
            published=max(t.at for t in ts), source=session)
