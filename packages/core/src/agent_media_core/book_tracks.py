"""A conversation as a library item that grows: one track per turn.

`book_export` lays a conversation out the way a podcast does — one file, one
chapter per turn — and that file is rebuilt from scratch every time the
conversation gains a turn. It is the shape a *finished* thing has, and it costs
what finishing costs: nothing can be published until the conversation goes
quiet, an 80-minute reply history is re-concatenated to add two minutes to it,
and every client that already fetched the old file is holding something that
has silently changed underneath it.

This lays the same conversation out as what it actually is: an item that
**appends**. Each turn is its own track, written once, never rewritten:

    <root>/<workspace>/<title>/001 - <first sentence>.mp3
                              /002 - <first sentence>.mp3

Measured against Audiobookshelf 2.35.1 before it was written (see
`docs/proposals/2026-09-02-growing-item-experiment.md`): a scan of a folder
that gained a file keeps the same item id, appends the track at the right
offset, leaves the existing files' inode, index and mtime alone, and preserves
the listener's position — which ABS stores in seconds, so it survives the
duration changing underneath it. The one thing it does not do by itself is
re-open an item the listener had finished; `reopen` below is that, and it is
two calls in an order the API does not advertise.

**One track per turn, not per sentence.** The renderer splits a reply into a
clip per sentence, so one conversation here is 686 clips across 80 minutes.
As tracks that is a list no player wants to show and no person wants to scroll,
and it would make Audiobookshelf keep 686 offsets for one item. A turn is the
unit that actually lands atomically, it is what "something new arrived" means,
and it makes the track list read like the conversation. A turn's own clips are
joined once, when the turn is already over, and never touched again.

The join is a stream copy where it can be. A turn whose clips disagree about
sample rate concatenates into a file that plays and lies about its length, so
the result is measured and re-encoded when it does not match — the same guard
`session_feed.build` carries, for the same reason.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from . import session_feed
from .book_export import root as book_export_root, safe_name
from ._paths import state_dir

log = logging.getLogger(__name__)


#: Appended to the workspace, which is the author a book library reads off the
#: folder. The growing items share the tree the concatenated ones live in —
#: that tree is what the scanner is already pointed at, and a second one would
#: mean a second mount and a second library for a trial. So they share the
#: shelf and differ by author: "p-agent-media" holds the finished files,
#: "p-agent-media (live)" holds the same conversations as items that append.
#: `book_export.export` knows to leave these alone when it prunes; without
#: that they would last until the next publish.
LIVE_SUFFIX = " (live)"


def root() -> Path:
    """Where the growing library lives. `MEDIA_BOOK_TRACKS_ROOT` overrides;
    otherwise the book tree `book_export` already writes, one author over."""
    raw = os.environ.get("MEDIA_BOOK_TRACKS_ROOT", "").strip()
    return Path(raw).expanduser() if raw else book_export_root()


def _manifest_path(session: str) -> Path:
    return state_dir() / "book-tracks" / f"{safe_name(session, 80)}.json"


def _read_manifest(session: str) -> dict:
    try:
        d = json.loads(_manifest_path(session).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_manifest(session: str, data: dict) -> None:
    p = _manifest_path(session)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(p)
    except OSError as e:
        log.warning("book-tracks: cannot write %s (%s)", p, e)


def _ffprobe(p: Path) -> float:
    return session_feed._ffprobe_duration(p)


def join_clips(clips: list, out: Path, expected: float = 0.0) -> Optional[Path]:
    """One turn's clips as one file. `out` on success, None on failure.

    A single clip is hardlinked rather than re-encoded: it is already exactly
    the bytes this track should hold, the spool keeps the only durable copy,
    and a link is one inode with two names. Cross-device falls back to a copy,
    because a link that cannot be made is not a reason to have no track.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    if len(clips) == 1:
        src = Path(clips[0])
        try:
            os.link(src, out)
        except OSError:
            try:
                shutil.copyfile(src, out)
            except OSError as e:
                log.warning("book-tracks: cannot place %s (%s)", src, e)
                return None
        return out

    with tempfile.TemporaryDirectory(prefix="media-turn-") as tmp:
        listing = Path(tmp) / "parts.txt"
        listing.write_text("".join(f"file '{Path(c).as_posix()}'\n" for c in clips))

        def _run(codec: list) -> bool:
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error",
                     "-f", "concat", "-safe", "0", "-i", str(listing),
                     *codec, str(out)],
                    check=True, capture_output=True, timeout=600)
            except (OSError, subprocess.SubprocessError) as e:
                log.warning("book-tracks: ffmpeg failed (%s)", e)
                return False
            return out.exists() and out.stat().st_size > 0

        if not _run(["-c", "copy"]):
            return None
        # Same measurement session_feed.build makes, for the same reason: a
        # concat of mixed sample rates plays and lies about its length, and a
        # track whose duration is wrong puts every later offset in the item
        # somewhere it is not.
        got = _ffprobe(out)
        if expected > 0 and abs(got - expected) > max(1.0, expected * 0.02):
            log.info("book-tracks: %.1fs ≠ %.1fs expected — re-encoding",
                     got, expected)
            if not _run(["-c:a", "libmp3lame", "-q:a", "4"]):
                return None
    return out


def track_name(index: int, turn) -> str:
    """`007 - First sentence of the turn.mp3`.

    The index leads because a scanner orders an item's files by name, and it is
    zero-padded because otherwise track 10 sorts before track 2 — which does
    not corrupt anything, it just plays the conversation in the wrong order,
    quietly.
    """
    return f"{index:03d} - {safe_name(turn.title, 90)}.mp3"


def folder_for(session: str, turns: list, manifest: dict) -> Optional[Path]:
    """The item folder for this conversation, decided once and then kept.

    The title comes from what was asked, so it can change as a conversation
    grows — and a library item that renames itself is a *new* item to
    Audiobookshelf: new id, no progress, and the old one left behind as a
    duplicate. So the first export writes the folder into the manifest and
    every later one uses it, even when a better title exists by then. An item
    that keeps its identity is worth more than an item with the best name.
    """
    kept = manifest.get("folder")
    if kept:
        return Path(kept)
    if not turns:
        return None
    workspace = session_feed.workspace_for(session, turns)
    title = session_feed.title_for(session, turns)
    return root() / (safe_name(workspace) + LIVE_SUFFIX) / safe_name(title)


def export_session(session: str, *, store=None) -> tuple[Optional[Path], int]:
    """Write any turns this conversation has gained. `(folder, added)`.

    Idempotent and append-only: a turn already on disk is identified by the
    `started_at` the history row carries, and is neither re-joined nor
    re-named. Nothing that exists is ever rewritten, which is the whole
    property that lets a client hold a downloaded copy while the conversation
    goes on.
    """
    turns = session_feed.turns(session, store=store)
    manifest = _read_manifest(session)
    folder = folder_for(session, turns, manifest)
    if folder is None:
        return None, 0

    done = {float(t["at"]): t for t in manifest.get("turns", [])}
    written = list(manifest.get("turns", []))
    added = 0
    for i, turn in enumerate(turns, start=1):
        at = float(turn.at)
        if at in done:
            continue
        expected = sum(turn.durations) or 0.0
        dest = folder / track_name(i, turn)
        if dest.exists():
            # A file we did not record — a manifest lost while the tree
            # survived. Adopt it rather than writing beside it under another
            # name, which is how an item ends up playing a turn twice.
            written.append({"at": at, "file": dest.name,
                            "dur": _ffprobe(dest)})
            continue
        if join_clips(turn.clips, dest, expected) is None:
            # Stop at the first failure: turns are ordered, and skipping one
            # would put the next turn's audio at this one's index.
            log.warning("book-tracks: %s stopped at turn %d", session, i)
            break
        written.append({"at": at, "file": dest.name,
                        "dur": expected or _ffprobe(dest)})
        added += 1

    if added or not manifest:
        manifest.update({"session": session, "folder": str(folder),
                         "turns": written})
        _write_manifest(session, manifest)
    return folder, added


def export_all(store=None, since_hours: float = 24.0) -> list:
    """Conversations that have said something lately. `[(session, folder, added)]`.

    Bounded on purpose. Speech history goes back months, and an unbounded run
    would build a growing item for every conversation that ever spoke — a
    library of hundreds where the point is the handful still being had. A
    conversation quiet for longer than the window is finished as far as this is
    concerned, and `book_export` already publishes those. `since_hours <= 0`
    means all of them, for the one-off backfill.
    """
    import time as _time

    cutoff = (_time.time() - since_hours * 3600.0) if since_hours > 0 else 0.0
    out = []
    for conv in session_feed.conversations(store=store):
        session = conv.get("session") or ""
        if not session:
            continue
        last = float(conv.get("last") or conv.get("at") or 0.0)
        if cutoff and last and last < cutoff:
            continue
        folder, added = export_session(session, store=store)
        if folder is not None:
            out.append((session, folder, added))
    return out


# --- the item, once the files are there ------------------------------------
#
# Appending a file is not the whole job. Audiobookshelf stores progress in
# seconds, so a listener's position survives the item growing — but `isFinished`
# survives it too. Reach the end of what exists, let a turn land, and the item
# stays finished: the new turn is on the server, correctly placed, and out of
# Continue Listening, which is the one place anyone would look for it. Measured
# on 2.35.1; see the experiment write-up.
#
# Re-opening it is two calls, and the order is not decoration: clearing
# `isFinished` in the same body as a position RESETS `currentTime` to zero,
# because ABS reads un-finishing as starting over. So clear the flag, then put
# the listener back — at the head of the turn they have not heard.

def _abs_items(url: str, token: str, lib_id: str) -> list:
    import json as _json
    import urllib.request as _u
    req = _u.Request(f"{url}/api/libraries/{lib_id}/items?limit=1000",
                     headers={"Authorization": f"Bearer {token}"})
    with _u.urlopen(req, timeout=15) as r:
        return _json.loads(r.read()).get("results", [])


def _abs_patch(url: str, token: str, path: str, body: dict) -> None:
    import json as _json
    import urllib.request as _u
    req = _u.Request(url + path, data=_json.dumps(body).encode(), method="PATCH",
                     headers={"Authorization": f"Bearer {token}",
                              "Content-Type": "application/json"})
    with _u.urlopen(req, timeout=15):
        return


def reopen(folder: Path, *, target=None) -> Optional[str]:
    """Bring a grown item back into Continue Listening. Item id, or None.

    None covers every ordinary case as well as failure: no Audiobookshelf
    configured, no item scanned for this folder yet, or a listener who had not
    finished it — that last one needs nothing done, because a position mid-item
    already survives the append on its own.
    """
    from . import library

    url, token, want = library._abs_cfg(target)
    if not url or not token:
        return None
    try:
        import json as _json
        import urllib.request as _u
        req = _u.Request(f"{url}/api/libraries",
                         headers={"Authorization": f"Bearer {token}"})
        with _u.urlopen(req, timeout=15) as r:
            libs = _json.loads(r.read()).get("libraries", [])
        lib = next((l for l in libs
                    if l.get("id") == want or l.get("name") == want), None) if want else None
        lib = lib or next((l for l in libs if l.get("mediaType") == "book"), None)
        if not lib:
            return None

        # Match on the tail of the path rather than the whole of it: the
        # server sees its mount ("/conversations/..."), this process sees the
        # host's ("/home/ryer/conversations/..."), and nothing here is told how
        # one maps onto the other. <author>/<title> is the part both agree on.
        tail = "/".join(folder.parts[-2:])
        item = next((i for i in _abs_items(url, token, lib["id"])
                     if str(i.get("path", "")).replace("\\", "/").endswith(tail)), None)
        if not item:
            return None

        req = _u.Request(f"{url}/api/me/progress/{item['id']}",
                         headers={"Authorization": f"Bearer {token}"})
        with _u.urlopen(req, timeout=15) as r:
            prog = _json.loads(r.read() or b"{}")
        if not prog.get("isFinished"):
            return None

        at = float(prog.get("currentTime") or 0.0)
        duration = float(item.get("media", {}).get("duration") or 0.0)
        _abs_patch(url, token, f"/api/me/progress/{item['id']}",
                   {"isFinished": False})
        body = {"currentTime": at}
        if duration > 0:
            body["duration"] = duration
            body["progress"] = min(1.0, at / duration) if duration else 0.0
        _abs_patch(url, token, f"/api/me/progress/{item['id']}", body)
        log.info("book-tracks: reopened %s at %.0fs of %.0fs", tail, at, duration)
        return item["id"]
    except Exception as e:  # noqa: BLE001 - a library that will not answer is not this job's problem
        log.warning("book-tracks: reopen failed (%s)", e)
        return None
