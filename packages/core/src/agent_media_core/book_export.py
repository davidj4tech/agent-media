"""The same conversations, laid out as books.

A podcast episode and an audiobook are the same file here — one mp3 with a
chapter per turn. What differs is what a client will do with it: Audiobookshelf
treats chapters as first-class for books and second-class for podcast episodes,
and on the Android app that means the chapter list a conversation is entirely
made of does not navigate. So the feed stays as the delivery mechanism, and
this lays the same episodes out as a library ABS can scan:

    <root>/<workspace>/<title>/<title>.mp3

Author is the workspace, title is the conversation. That is not a hack around
ABS's scanner so much as the same grouping the feeds already use, expressed in
the one vocabulary a book library has.

**Hardlinks, not copies.** The spool already holds the only durable copy of
this audio; a second byte-for-byte copy on the same filesystem buys nothing and
doubles what a long conversation costs. A link is one inode with two names, so
the library and the spool cannot drift, and deleting one never takes the audio
with it. Cross-device falls back to copying, because a link that cannot be made
is not a reason to have no library.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Optional

from . import feed as feedmod
from ._paths import state_dir

log = logging.getLogger(__name__)

#: Feeds that are not conversations. A document read aloud is a document; it
#: has no workspace and no turns, and it belongs in the feed it already has.
SKIP_FEEDS = frozenset({"docs", "digest"})

#: Authors this tree does not own. Defined here rather than imported from
#: `book_tracks` so the prune cannot be made to depend on the module whose work
#: it is protecting; the two must agree, and the test says so.
LIVE_SUFFIX = " (live)"


def root() -> Path:
    """Where the book tree lives. `MEDIA_BOOK_EXPORT_ROOT` overrides."""
    raw = os.environ.get("MEDIA_BOOK_EXPORT_ROOT", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / "conversations"


#: A folder name a scanner and three filesystems can all live with. Not the
#: episode title verbatim: those carry `/`, `:` and the em dash that separates
#: workspace from question.
_UNSAFE = re.compile(r"[^\w .,()'’-]+")


def safe_name(text: str, limit: int = 110) -> str:
    name = _UNSAFE.sub(" ", (text or "").replace("·", "-")).strip()
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > limit:
        name = name[:limit].rsplit(" ", 1)[0].rstrip(" .,-")
    return name or "conversation"


def _link_or_copy(src: Path, dest: Path) -> bool:
    """True if `dest` now holds `src`'s bytes and is new or unchanged."""
    if dest.exists():
        try:
            if dest.stat().st_size == src.stat().st_size:
                return False            # already there, nothing to do
        except OSError:
            pass
        dest.unlink(missing_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dest)
    except OSError:
        # Different filesystem, or a filesystem without links. A copy is worse
        # — it can drift, and it doubles the disk — but it is not nothing.
        shutil.copyfile(src, dest)
    return True


def export(where: Optional[Path] = None) -> tuple[int, int]:
    """Mirror every conversation episode into the book tree.

    Returns (linked, removed). Idempotent: an episode already present is left
    alone, and a folder whose episode has gone — pruned by retention, or
    republished under another workspace — is taken out, so the library is the
    feeds and not a scrapbook of everything they ever held.
    """
    where = where or root()
    where.mkdir(parents=True, exist_ok=True)
    wanted: dict[Path, Path] = {}

    for name in feedmod.feeds():
        if name in SKIP_FEEDS:
            continue
        for ep in feedmod.episodes(name):
            src = feedmod.feed_dir(name) / ep.filename
            if not src.is_file():
                continue
            # The workspace is already the feed's name, and the title repeats
            # it ("p-agent-media · why…"); strip that back off so the folder
            # does not say it twice.
            title = ep.title.split(" · ", 1)[-1] if " · " in ep.title else ep.title
            folder = where / safe_name(name) / safe_name(title)
            wanted[folder / (safe_name(title) + src.suffix)] = src

    linked = sum(1 for dest, src in wanted.items() if _link_or_copy(src, dest))

    removed = 0
    keep_dirs = {p.parent for p in wanted}
    for author in sorted(p for p in where.iterdir() if p.is_dir()):
        # The growing items (book_tracks) share this shelf under their own
        # author. They are not built from feed episodes, so nothing here can
        # ever say it wants them, and the sweep below would take every one on
        # its next run — a conversation deleted mid-listen for tidiness.
        if author.name.endswith(LIVE_SUFFIX):
            continue
        for book in sorted(p for p in author.iterdir() if p.is_dir()):
            if book in keep_dirs:
                continue
            shutil.rmtree(book, ignore_errors=True)
            removed += 1
        try:
            author.rmdir()          # only if it is now empty
        except OSError:
            pass
    return linked, removed
