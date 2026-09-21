"""Claude Code's own recaps: the "while you were away" paragraph.

When you come back to a Claude Code session after being away, it writes a
short summary of where things stand into the session's transcript, as

    {"type": "system", "subtype": "away_summary",
     "content": "<a paragraph> (disable recaps in /config)",
     "timestamp": "2026-09-21T07:01:52.291Z", "uuid": …, "parentUuid": …}

It is not a message — nobody said it, and it is not in the conversation the
agent sees — so it never becomes a line in `/conversation/log`. The app shows
the latest one as a card at the top of the thread and as the list's preview
line (server-contract.md §6.1, §6.2, §14).

Only Claude Code writes these. Codex, pi and Hermes have nothing like it, so
their sessions answer None here. The idle reaper writes a recap of its own
through the follow-up gateway call before it rests a session (`rest.py`), and
`recap_for` — what the routes use — picks the newer of the two and says whose
it is (`"source": "claude" | "agent-media"`).

Read-only: this never writes to a transcript.

**Cost.** A transcript is 1–10 MB and `/targets` asks about ~44 of them on
every open, so nothing here parses a whole file per request:

* The latest recap is found by reading **backwards** from the end in chunks,
  and only lines containing the marker are parsed. A recap near the end costs
  one chunk.
* Each file's answer is cached with the inode and the offset it was read up
  to. Transcripts are append-only JSONL, so when a live session's file grows
  only the appended bytes are read; a recap there replaces the cached one,
  otherwise the cached one stands. A file that shrank, was replaced (new
  inode) or was rewritten in place (the bytes before the old offset differ,
  `_seam`) is read again from scratch.
* A file with no recap at all is read once, end to start, and from then on
  only as it grows. That first read is the expensive case (see the timing in
  the commit that added this).
"""

from __future__ import annotations

import glob
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

#: Every recap line has this; a line without it is never parsed.
_MARK = b"away_summary"
#: Read size when walking backwards. Big enough that a recap near the end is
#: one read, small enough that the common case stays cheap.
_CHUNK = 256 * 1024
#: The hint Claude Code appends: " (disable recaps in /config)". Matched
#: conservatively — a trailing parenthetical that talks about recaps — so a
#: summary that happens to end in an ordinary "(…)" keeps it.
_HINT = re.compile(r"\s*\([^()]*\brecaps?\b[^()]*\)\s*$", re.IGNORECASE)
#: How many transcripts' answers to keep. Far more than /targets lists; the
#: bound is only there so a long-running canvas cannot grow without limit.
_CACHE_MAX = 512
#: How many bytes before the read offset are kept to tell an append from a
#: rewrite in place. A transcript line ends in a uuid and a timestamp, so
#: this much of it is as good as unique.
_SEAM = 256


class _Entry:
    """What one transcript file is known to hold, up to `offset`."""

    __slots__ = ("ino", "size", "mtime", "offset", "seam", "latest", "all")

    def __init__(self, ino: int, size: int, mtime: float, offset: int, seam: bytes,
                 latest: dict | None, all_: list | None):
        # (inode, size, mtime) is the file as last seen: all three the same
        # means nothing to read.
        self.ino = ino
        self.size = size
        self.mtime = mtime
        self.offset = offset       # bytes read, always at a line boundary
        self.seam = seam           # the bytes just before `offset` (`_seam`)
        self.latest = latest       # newest recap in [0, offset), or None
        self.all = all_            # every recap in [0, offset), or None: not listed yet


_CACHE: dict[str, _Entry] = {}
#: Session id → transcript path, so a known session is a stat, not a glob.
_PATHS: dict[str, str] = {}
_LOCK = threading.Lock()


def _reset_for_tests() -> None:
    with _LOCK:
        _CACHE.clear()
        _PATHS.clear()


# --- finding the file ------------------------------------------------------------


def _claude_dir() -> Path:
    from agent_media_core import harnesses

    return harnesses._claude_dir()


def transcript_path(session: str) -> str:
    """The Claude Code transcript for `session`, or "" when it has none.

    Codex and pi ids are uuids too, and simply have no file here; a Hermes id
    cannot be a Claude session at all, so it is not even looked for.
    """
    from agent_media_core import harnesses

    if not harnesses.SESSION_ID.fullmatch(session or "") or harnesses.is_hermes(session):
        return ""
    with _LOCK:
        known = _PATHS.get(session)
    if known and os.path.exists(known):
        return known
    hits = glob.glob(str(_claude_dir() / "projects" / "*" / f"{session}.jsonl"))
    if not hits:
        return ""
    # A session copied between project dirs (a moved checkout) is written to
    # in one of them: the one written last.
    path = max(hits, key=lambda p: os.path.getmtime(p))
    with _LOCK:
        _PATHS[session] = path
    return path


# --- reading a recap line ---------------------------------------------------------


def _epoch(ts: str) -> float | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return round(dt.timestamp(), 3)


def _text(content) -> str:
    """The summary's words: a plain string today, text blocks just in case."""
    if isinstance(content, list):
        content = " ".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
    return _HINT.sub("", str(content or "")).strip()


def _parse(raw: bytes) -> dict | None:
    """`{"text", "at"}` from one transcript line, or None if it is not a recap.

    A torn or malformed line (a writer mid-append, a bad byte) is not a recap.
    """
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(rec, dict) or rec.get("type") != "system" \
            or rec.get("subtype") != "away_summary":
        return None
    text = _text(rec.get("content"))
    at = _epoch(rec.get("timestamp") or "")
    if not text or at is None:
        return None
    return {"text": text, "at": at}


# --- scanning ---------------------------------------------------------------------


def _last_in(fh, lo: int, hi: int) -> dict | None:
    """The last recap in bytes [lo, hi), reading backwards in chunks.

    `lo` and `hi` are line boundaries. Each chunk's first, partial line is
    carried over to the next (earlier) read, so a line split across two
    chunks is looked at whole.
    """
    carry = b""
    pos = hi
    while pos > lo:
        n = min(_CHUNK, pos - lo)
        pos -= n
        fh.seek(pos)
        buf = fh.read(n) + carry
        if pos > lo:
            cut = buf.find(b"\n")
            if cut < 0:
                carry = buf               # one line longer than a chunk
                continue
            carry, buf = buf[:cut], buf[cut + 1:]
        else:
            carry = b""
        end = len(buf)
        while True:
            i = buf.rfind(_MARK, 0, end)
            if i < 0:
                break
            s = buf.rfind(b"\n", 0, i) + 1
            e = buf.find(b"\n", i)
            found = _parse(buf[s:e if e >= 0 else len(buf)])
            if found:
                return found
            end = s
    return None


def _all_in(fh, lo: int, hi: int) -> list[dict]:
    """Every recap in bytes [lo, hi), oldest first. Only marked lines are parsed."""
    fh.seek(lo)
    buf = fh.read(hi - lo)
    out = []
    i = buf.find(_MARK)
    while i >= 0:
        s = buf.rfind(b"\n", 0, i) + 1
        e = buf.find(b"\n", i)
        e = len(buf) if e < 0 else e
        found = _parse(buf[s:e])
        if found:
            out.append(found)
        i = buf.find(_MARK, e)
    return out


def _complete_end(fh, size: int) -> int:
    """The offset just past the last newline — a line still being written is
    left for the next read, which will see it whole."""
    pos = size
    while pos > 0:
        n = min(4096, pos)
        fh.seek(pos - n)
        chunk = fh.read(n)
        k = chunk.rfind(b"\n")
        if k >= 0:
            return pos - n + k + 1
        pos -= n
    return 0


def _seam(fh, offset: int) -> bytes:
    """The bytes just before `offset`. Still the same on the next read means
    the file was appended to; different means it was rewritten in place (same
    inode, not shorter), and the offset no longer means anything."""
    lo = max(0, offset - _SEAM)
    fh.seek(lo)
    return fh.read(offset - lo)


def _entry(path: str, listing: bool) -> _Entry | None:
    """The cached answer for `path`, brought up to date.

    `listing` also fills `all` (every recap), which the latest-only callers
    never pay for.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    with _LOCK:
        old = _CACHE.get(path)
    if old and (old.ino, old.size, old.mtime) == (st.st_ino, st.st_size, st.st_mtime) \
            and (old.all is not None or not listing):
        # Unchanged since last read. (A torn tail line left unread last time
        # changes the size when it completes.)
        return old
    try:
        with open(path, "rb") as fh:
            end = _complete_end(fh, st.st_size)
            if old and old.ino == st.st_ino and old.offset <= end \
                    and _seam(fh, old.offset) == old.seam:
                # Appended to: read only what is new.
                latest = _last_in(fh, old.offset, end) or old.latest
                if listing and old.all is None:
                    all_ = _all_in(fh, 0, end)
                elif old.all is not None:
                    all_ = old.all + _all_in(fh, old.offset, end)
                else:
                    all_ = None
            else:
                # First sight, shrunk, or replaced: from scratch.
                latest = _last_in(fh, 0, end)
                all_ = _all_in(fh, 0, end) if listing else None
            seam = _seam(fh, end)
    except OSError:
        return None
    new = _Entry(st.st_ino, st.st_size, st.st_mtime, end, seam, latest, all_)
    with _LOCK:
        _CACHE.pop(path, None)
        _CACHE[path] = new
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
    return new


# --- the answers ------------------------------------------------------------------


def latest_recap(session: str) -> dict | None:
    """`{"text", "at"}` — the newest recap in `session`'s transcript — or None.

    None for a session with no recap yet, for anything that is not a Claude
    Code session, and for a transcript that cannot be read.
    """
    path = transcript_path(session)
    if not path:
        return None
    e = _entry(path, listing=False)
    return dict(e.latest) if e and e.latest else None


def recaps(session: str, since: float | None = None) -> list[dict]:
    """Every recap in `session`'s transcript, oldest first; with `since`, only
    those after it. [] for anything `latest_recap` would answer None for.

    Not on any route yet: the app shows one card, the latest. This is here so
    a history of them costs nothing new when something wants it.
    """
    path = transcript_path(session)
    if not path:
        return []
    e = _entry(path, listing=True)
    rows = list(e.all or []) if e else []
    if since is not None:
        rows = [r for r in rows if r["at"] > since]
    return [dict(r) for r in rows]


def recap_for(session: str) -> dict | None:
    """The recap a row or a log shows: `{"text", "at", "source"}`, or None.

    `source` is "claude" for Claude Code's own away_summary and "agent-media"
    for the one the reaper wrote before resting the session (`rest.py`). The
    newer of the two wins, so Claude's own is shown whenever it is at least as
    recent — and a Codex, pi or Hermes session, which has no Claude recap at
    all, shows the generated one when there is one.
    """
    from . import rest

    own = latest_recap(session)
    ours = rest.generated_recap(session)
    if ours and (not own or ours["at"] > own["at"]):
        return {**ours, "source": "agent-media"}
    if own:
        return {**own, "source": "claude"}
    return None
