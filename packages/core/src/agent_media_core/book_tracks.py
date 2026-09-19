"""A conversation as a library item that grows: the clips, as they land.

What this replaced built one file per conversation — every clip concatenated,
one chapter per turn — and rebuilt it from scratch each time another turn
landed. Nothing could be published until the conversation had been quiet for an
hour, an 80-minute history was re-concatenated to add two minutes to it, and
anyone holding the old file was holding something that had changed underneath
them. It also cost a second copy of every conversation: 91MB of spool beside
the clips it was made from.

Measured against Audiobookshelf 2.35.1 before any of this was written (see
`docs/proposals/2026-09-02-growing-item-experiment.md`): a scan of a folder
that gained a file keeps the same item id, appends the track at the right
offset, leaves the existing files' inode, index and mtime alone, and preserves
the listener's position — which ABS stores in seconds, so it survives the
duration changing underneath it. The one thing it does not do by itself is
re-open an item the listener had finished — nor to revise the duration it
recorded in that listener's progress. `sync_progress` below is both, and the
re-open half is two calls in an order the API does not advertise.

**The clips are the tracks.** The renderer already splits a reply into a clip
per sentence, and those files already exist — so this writes no audio at all.
Each track is a *hardlink* to the clip the renderer made: one inode with two
names, nothing to re-encode, nothing to keep in step, and a conversation costs
the library zero bytes. Joining each turn into one file was tried first and
undone: it wrote a second copy of every conversation, needed ffmpeg and a
guard against concatenations that lie about their length, and bought only a
shorter list.

That list is the visible trade. Audiobookshelf makes a chapter of every track,
so a conversation is chaptered by *sentence* — 336 of them for this one. Which
is either a lot of rows, or a transcript you can jump around in; the sentence
map the renderer recorded is what makes it the second, because each chapter is
named with the words it is about to say.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from . import session_feed
from ._paths import state_dir

log = logging.getLogger(__name__)


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


def root() -> Path:
    """Where the library lives. `MEDIA_BOOK_TRACKS_ROOT` overrides, and
    `MEDIA_BOOK_EXPORT_ROOT` still does too — it named this tree first, and a
    host that set it meant this tree."""
    raw = (os.environ.get("MEDIA_BOOK_TRACKS_ROOT", "").strip()
           or os.environ.get("MEDIA_BOOK_EXPORT_ROOT", "").strip())
    return Path(raw).expanduser() if raw else Path.home() / "conversations"


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


def place_clip(src, out: Path) -> Optional[Path]:
    """Give the clip a second name inside the item. `out`, or None.

    A hardlink, not a copy: the render cache holds the only durable copy of
    this audio, and a byte-for-byte duplicate would double what a conversation
    costs while being able to drift from it. Two names for one inode cannot
    disagree, and deleting either never takes the audio with it. Cross-device
    falls back to copying, because a link that cannot be made is not a reason
    to have no track.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    src = Path(src)
    try:
        os.link(src, out)
    except FileExistsError:
        return out
    except OSError:
        try:
            shutil.copyfile(src, out)
        except OSError as e:
            log.warning("book-tracks: cannot place %s (%s)", src, e)
            return None
    return out


def track_name(index: int, said: str, fallback: str = "") -> str:
    """`007 - The sentence this clip says.mp3`.

    The index leads because a scanner orders an item's files by name, and it is
    zero-padded to four because otherwise track 10 sorts before track 2 — which
    corrupts nothing, it just plays the conversation in the wrong order,
    quietly. Four digits, because a long conversation is thousands of
    sentences and the day that wraps is the day every later one is misfiled.
    """
    return f"{index:04d} - {safe_name(said or fallback, 90)}.mp3"


def throwaway_workspace(workspace: str) -> bool:
    """A session run from a temp dir, which no library should shelve.

    A session that tests something by starting another Claude in its scratch
    folder leaves a tmux session named after that path
    (`tmp-claude-1000--home-…-scratchpad-handoff-test`), and every one became a
    project of its own on the Projects shelf. `scratch` is not one of these:
    that is a workspace David keeps on purpose.
    """
    return workspace.startswith("tmp-") or "-scratchpad" in workspace


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
    if throwaway_workspace(workspace):
        return None
    # The workspace is the folder above, which Audiobookshelf reads as the
    # author — so putting it in the title too says it twice on every shelf.
    title = session_feed.asked_for(session, turns)
    return root() / safe_name(workspace) / safe_name(title)


# --- the listener's own turns -------------------------------------------------
#
# A conversation you can reply to is a conversation with two people in it, and
# only one of them was being recorded. The reply box put words into the session
# and they vanished — the answer came back on the shelf, the question did not,
# and a chapter list of answers to invisible questions is a worse record than
# no record.
#
# So a typed reply is rendered to speech and written to speech history like any
# other turn. It is not *played*: nobody wants their own message read at them
# as they send it. The row is what matters — the exporter reads history, so the
# turn lands in order, in the right item, with a chapter of its own, and the
# audio is there for whoever plays the conversation back later.
#
# In a different voice, deliberately. The whole representation is audio, so the
# only way to hear who is speaking is to hear it.

#: Australian, and not the assistant's. Override with MEDIA_LISTENER_VOICE.
LISTENER_VOICE = "en-AU-WilliamNeural"


#: How long the same words from the same listener count as the same turn.
LISTENER_REPEAT_S = 120.0


def _claim_listener_turn(session: str, text: str, now: float,
                         store=None) -> bool:
    """Claim `text` for this session; False if someone already has.

    The history check below is not enough on its own: the two recorders start
    within the same second and each spends one or two rendering before it
    writes, so both look, see nothing, and both record. A claim file, created
    exclusively before any of that, is decided at once. Old claims are swept
    as they are passed, so the directory never grows past a day's replies.
    """
    import hashlib

    d = state_dir() / "listener-claims"
    try:
        d.mkdir(parents=True, exist_ok=True)
        for p in d.iterdir():
            try:
                if now - p.stat().st_mtime > LISTENER_REPEAT_S:
                    p.unlink()
            except OSError:
                pass
        flat = " ".join(text.split())
        key = hashlib.sha1(f"{session}\n{flat}".encode()).hexdigest()[:24]
        fd = os.open(str(d / key), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        os.close(fd)
        return True
    except FileExistsError:
        # Held — but a claim from before the session last spoke belongs to the
        # earlier turn. "y", a reply, "y" again is two answers, not a repeat.
        claim = d / key
        try:
            since = claim.stat().st_mtime
        except OSError:
            return False
        if store is not None and _session_spoke_since(store, session, text, since):
            try:
                os.utime(claim, (now, now))
            except OSError:
                pass
            return True
        return False
    except OSError:
        # A state dir that cannot take a file cannot dedupe; the history check
        # still stands, and a repeat is better than a lost turn.
        return True


def _flat(text) -> str:
    return " ".join(str(text or "").split())


def _session_spoke_since(store, session: str, text: str, since: float) -> bool:
    """Whether anything but `text` was said in this session after `since`."""
    try:
        rows = store.recent_history(sink="speech", limit=50)
    except Exception:  # noqa: BLE001 — no answer, so no evidence of a new turn
        return False
    label = _flat(f"You: {text}")
    for row in rows:
        ex = row.get("extras")
        if not isinstance(ex, dict) or ex.get("source_session") != session:
            continue
        if _flat(row.get("text")) != label and float(row.get("started_at") or 0) > since:
            return True
    return False


def _listener_turn_recently(store, session: str, text: str, now: float) -> bool:
    """Whether `text` was already recorded as this session's listener turn.

    Only the latest thing said in the session counts. The repeat this catches
    — the canvas and the prompt hook recording one reply — arrives with
    nothing in between; the same short answer given twice ("y", then "y" to
    the next question) has the assistant's reply between, and is a new turn.
    """
    try:
        rows = store.recent_history(sink="speech", limit=50)
    except Exception:  # noqa: BLE001 — a store that cannot answer cannot dedupe
        return False
    label = _flat(f"You: {text}")
    for row in rows:  # newest first
        ex = row.get("extras")
        if not isinstance(ex, dict) or ex.get("source_session") != session:
            continue
        if not ex.get("listener") or _flat(row.get("text")) != label:
            return False
        return now - float(row.get("started_at") or 0) <= LISTENER_REPEAT_S
    return False


def record_listener_turn(session: str, text: str, *, store=None,
                         extras: Optional[dict] = None) -> bool:
    """Render a reply the listener typed and add it to the conversation.

    Returns whether the turn was recorded. A failed render is not fatal to the
    reply itself — the words still reached the session, which is the point of
    the feature; they just do not appear on the shelf.
    """
    import time as _time

    # Line breaks kept: a message typed over several lines reads that way in
    # the transcript. The repeat checks compare flattened copies, because the
    # canvas types a reply into the terminal on one line and the prompt hook
    # then sees it without the breaks the reply box recorded.
    from .intake._text import tidy_lines

    text = tidy_lines(text)
    if not text or not session:
        return False
    from ._paths import cache_dir
    from .render.engines import render_text
    from .state.store import StateStore

    at = _time.time()
    st = store or StateStore()
    # The same words arrive twice when a reply sent from the player is typed
    # into the session by the canvas: the canvas records it as it sends, and
    # the prompt hook records it as Claude Code receives it. Both are right to
    # try — either may be the only one installed — so the recorder is where
    # the repeat is dropped. Same session, same words, within a couple of
    # minutes: already a turn, and reported as one.
    if _listener_turn_recently(st, session, text, at):
        return True
    if not _claim_listener_turn(session, text, at, st):
        return True
    d = cache_dir() / "audio"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("book-tracks: no clip dir (%s)", e)
        return False
    stamp = _time.strftime("%Y%m%dT%H%M%S", _time.localtime(at))
    out = d / f"{stamp}-listener-{safe_name(session, 8)}.mp3"

    engine = os.environ.get("MEDIA_LISTENER_ENGINE") or "edge"
    voice = os.environ.get("MEDIA_LISTENER_VOICE") or LISTENER_VOICE
    ok, err = render_text(text, out, engine=engine, voice=voice,
                          edge_voice=voice)
    if not ok or not out.is_file():
        log.warning("book-tracks: could not render a listener turn (%s)", err)
        return False
    dur = _ffprobe(out)

    try:
        st.add_history(
            sink="speech", uri=str(out), started_at=at, ended_at=at + dur,
            # Never played anywhere: the row exists to be archived, not routed.
            target="none", source="listener", content_type="audio/mpeg",
            # The chapter title comes from this text, so it carries the speaker
            # label — a chapter list is read, not heard, and without it the
            # questions and the answers look alike. The audio says only the
            # reply; the label is not spoken.
            text=f"You: {text}",
            extras={"source_session": session, "listener": True,
                    "engine": engine, "voice": voice,
                    "clip_uris": [str(out)], "clip_sentences": [text],
                    "clip_durations_s": [dur],
                    # A slash command rides along as `command`, so the chat can
                    # show it as the instruction it is rather than a sentence
                    # somebody said out loud.
                    **(extras or {})})
    except Exception as e:  # noqa: BLE001 — the reply already landed
        log.warning("book-tracks: could not record a listener turn (%s)", e)
        return False

    # Put it on the shelf on the same schedule as a spoken turn, rather than
    # waiting for the answer to arm one: a question that appears after its
    # answer is worse than one that appears late.
    try:
        from . import feed_debounce

        feed_debounce.arm()
    except Exception:  # noqa: BLE001 — the poll will catch it
        pass
    return True


def export_session(session: str, *, store=None) -> tuple[Optional[Path], int]:
    """Write any clips this conversation has gained. `(folder, added)`.

    Idempotent and append-only. The turn is the unit that lands — a reply's
    clips all exist by the time it is over — so a turn already exported is
    identified by the `started_at` its history row carries and skipped whole.

    The running number comes from the manifest, never from recounting the
    turns. Speech history outlives the render cache, so a conversation can lose
    an old turn's audio entirely (`session_feed.turns` drops what it cannot
    find) — and a number derived by counting would then shift every track after
    the gap, renaming files that are already on disk and already downloaded.
    What has been written is what decides where the next one goes.
    """
    turns = session_feed.turns(session, store=store)
    manifest = _read_manifest(session)
    folder = folder_for(session, turns, manifest)
    if folder is None:
        return None, 0

    done = {float(t["at"]) for t in manifest.get("turns", [])}
    written = list(manifest.get("turns", []))
    index = sum(len(t.get("files") or []) for t in written)
    added = 0
    for turn in turns:
        at = float(turn.at)
        if at in done:
            continue
        files = []
        for i, clip in enumerate(turn.clips):
            said = turn.sentences[i] if i < len(turn.sentences) else ""
            dest = folder / track_name(index + 1, said, turn.title)
            if dest.exists():
                # Ours already, from a run whose manifest was lost. Adopt it
                # rather than counting it as new: "+12 tracks" for a run that
                # wrote nothing is a report nobody can act on.
                index += 1
                files.append(dest.name)
                continue
            if place_clip(clip, dest) is not None:
                index += 1
                files.append(dest.name)
                added += 1
                continue
            # Stop at the first clip that will not go: the numbering is
            # sequential, so carrying on would file the next sentence under
            # this one's number and leave the conversation out of order.
            log.warning("book-tracks: %s stopped at track %d", session, index + 1)
            break
        if not files:
            break
        written.append({"at": at, "title": turn.title, "files": files})

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
#
# The `duration` ABS wrote into that progress row survives the growth too, and
# wrongly — it is never revised, so a conversation paused two sentences in
# reads as two sentences long from then on. Re-baselining it is the same call,
# made for the listener who finished nothing.

def chapters_from(turns: list, tracks: list) -> list:
    """One chapter per turn, over tracks that are one per sentence.

    The two units are not in conflict, they answer different questions. The
    *tracks* are the clips the renderer made, because those files already exist
    and linking them costs nothing. The *chapters* are the turns, because that
    is what a listener moves around by — 352 rows of one sentence each is a
    transcript, not a table of contents.

    Offsets come from the tracks as the server computed them, never from the
    durations recorded here: the server is the thing that will play it, and if
    the two ever disagree the one that is wrong is this one.
    """
    out, i = [], 0
    for n, turn in enumerate(turns):
        count = len(turn.get("files") or [])
        if not count or i + count > len(tracks):
            break
        start = float(tracks[i].get("startOffset") or 0.0)
        last = tracks[i + count - 1]
        end = float(last.get("startOffset") or 0.0) + float(last.get("duration") or 0.0)
        title = (turn.get("title") or "").strip()
        if not title:
            # A manifest written before titles were kept. The filename holds
            # the sentence, which is the same words the title would have been.
            name = (turn.get("files") or [""])[0]
            title = name.rsplit(".", 1)[0].split(" - ", 1)[-1]
        out.append({"id": n, "start": round(start, 3), "end": round(end, 3),
                    "title": title[:120]})
        i += count
    return out


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


def _abs_ready(target=None):
    """`(url, token, [libraries])` for the book libraries, or None when there is
    no Audiobookshelf configured on this host — which is not a failure.

    Every book library, not the configured one. A host with two of them —
    "Audiobooks" and "Conversations" here — resolves an unset `ABS_LIBRARY` to
    whichever came first, and looking for a conversation in the audiobooks is a
    silent nothing: no item, no chapters, no complaint. The folder tail is
    unique across libraries anyway, so searching them all cannot be wrong; the
    configured one just goes first.
    """
    from . import library

    url, token, want = library._abs_cfg(target)
    if not url or not token:
        return None
    import json as _json
    import urllib.request as _u
    try:
        req = _u.Request(f"{url}/api/libraries",
                         headers={"Authorization": f"Bearer {token}"})
        with _u.urlopen(req, timeout=15) as r:
            libs = _json.loads(r.read()).get("libraries", [])
    except Exception:  # noqa: BLE001
        return None
    books = [l for l in libs if l.get("mediaType") == "book"]
    if want:
        books.sort(key=lambda l: (l.get("id") != want and l.get("name") != want))
    return (url, token, books) if books else None


def _abs_ready_all(target=None):
    """Every Audiobookshelf that should carry conversation metadata, as a list
    of `(url, token, [book libraries])`.

    The primary (`_abs_ready`) first, then each extra server from `ABS_SERVERS`
    resolved to its own book libraries with its own token. Empty when none is
    configured. A server that will not answer is dropped, not fatal — the same
    posture the single-server path always took.
    """
    from . import library

    out = []
    primary = _abs_ready(target)
    if primary:
        out.append(primary)
    import json as _json
    import urllib.request as _u
    for url, token in library._abs_extra_servers(target):
        try:
            req = _u.Request(f"{url}/api/libraries",
                             headers={"Authorization": f"Bearer {token}"})
            with _u.urlopen(req, timeout=15) as r:
                libs = _json.loads(r.read()).get("libraries", [])
        except Exception:  # noqa: BLE001
            continue
        books = [l for l in libs if l.get("mediaType") == "book"]
        if books:
            out.append((url, token, books))
    return out


def _find_item(url: str, token: str, libs: list, folder: Path):
    """The scanned item for this folder, or None.

    Match on the tail of the path rather than the whole of it: the server sees
    its mount ("/conversations/..."), this process sees the host's, and nothing
    here is told how one maps onto the other. <author>/<title> is the part both
    agree on.
    """
    tail = "/".join(folder.parts[-2:])
    for lib in libs:
        for i in _abs_items(url, token, lib["id"]):
            if str(i.get("path", "")).replace("\\", "/").endswith(tail):
                return i
    return None


def _abs_progress(url: str, token: str, item_id: str) -> Optional[dict]:
    """This listener's saved progress for an item, or None if they have none.

    None for a 404, which is the commonest answer of all — "nobody has played
    it" — and the one that used to be logged as a failure every half hour.
    """
    import json as _json
    import urllib.request as _u
    req = _u.Request(f"{url}/api/me/progress/{item_id}",
                     headers={"Authorization": f"Bearer {token}"})
    try:
        with _u.urlopen(req, timeout=15) as r:
            return _json.loads(r.read() or b"{}")
    except _u.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _abs_item(url: str, token: str, item_id: str) -> Optional[dict]:
    import json as _json
    import urllib.request as _u
    req = _u.Request(f"{url}/api/items/{item_id}",
                     headers={"Authorization": f"Bearer {token}"})
    try:
        with _u.urlopen(req, timeout=10) as r:
            return _json.loads(r.read())
    except Exception:  # noqa: BLE001
        return None


def _track_count(item: dict) -> int:
    """How many audio files ABS holds for an item.

    Two shapes: the library listing summarises (`media.numTracks`), while
    `/api/items/<id>` expands (`media.audioFiles`) and carries no count at all.
    Read whichever is there.
    """
    media = item.get("media") or {}
    files = media.get("audioFiles")
    if isinstance(files, list):
        return len(files)
    return int(media.get("numTracks") or 0)


def wait_for_tracks(folder: Path, want: int, *, target=None,
                    timeout_s: float = 25.0) -> bool:
    """Block until Audiobookshelf's item for `folder` holds `want` tracks.

    This replaces sleeping a fixed ten seconds after asking for a scan. The
    sleep was a guess in both directions: too long when the scan finished in
    two, and too short when it did not, in which case the chapters were written
    against the durations the item had *before* the turn landed. Asking the
    item how many tracks it has now is the actual question.

    False on timeout, which is not fatal — the caller writes chapters anyway
    and the next turn corrects them.
    """
    import time as _time

    ready = _abs_ready(target)
    if not ready:
        return False
    url, token, libs = ready
    item = _find_item(url, token, libs, folder)
    deadline = _time.monotonic() + timeout_s
    while True:
        if item:
            fresh = _abs_item(url, token, item["id"]) or item
            if _track_count(fresh) >= want:
                return True
        if _time.monotonic() >= deadline:
            return False
        _time.sleep(0.4)
        if not item:
            # The item did not exist yet at all — a conversation's first turn.
            item = _find_item(url, token, libs, folder)


def publish_chapters(session: str, folder: Path, *, target=None) -> int:
    """Give the item a chapter per turn. Number written, or 0.

    Audiobookshelf makes one chapter per audio file when it first scans a
    multi-track item, which for clips-as-tracks means a chapter per sentence —
    and on an item it later *updates*, it does not redo them at all: the item
    grows to 352 tracks and keeps the 32 chapters it was born with, pointing at
    moments that have moved. (That staleness is #525 upstream, and it is not
    worth waiting for.) So the chapters are written here rather than inferred
    there: the turn structure is in the manifest, the offsets are in the tracks
    the server just scanned, and the API takes the list.
    """
    ready = _abs_ready(target)
    if not ready:
        return 0
    url, token, libs = ready
    item = _find_item(url, token, libs, folder)
    if not item:
        return 0
    try:
        import json as _json
        import urllib.request as _u
        req = _u.Request(f"{url}/api/items/{item['id']}?expanded=1",
                         headers={"Authorization": f"Bearer {token}"})
        with _u.urlopen(req, timeout=20) as r:
            full = _json.loads(r.read())
        chapters = chapters_from(_read_manifest(session).get("turns", []),
                                 full.get("media", {}).get("tracks") or [])
        if not chapters:
            return 0
        body = _json.dumps({"chapters": chapters}).encode()
        req = _u.Request(f"{url}/api/items/{item['id']}/chapters", data=body,
                         method="POST",
                         headers={"Authorization": f"Bearer {token}",
                                  "Content-Type": "application/json"})
        with _u.urlopen(req, timeout=20):
            pass
        return len(chapters)
    except Exception as e:  # noqa: BLE001
        log.warning("book-tracks: chapters failed (%s)", e)
        return 0


def _live_turn(session: str) -> Optional[dict]:
    """The turn being spoken *right now* for `session`, as
    `{at, text, listener}`, or None.

    History is only written when a turn ends, so for the whole time a reply is
    audible its words are missing from speech history — which is why the
    transcript could not show a turn until after it finished speaking. This
    reads the same now_playing row the speech controls use, with the same
    writer-pid liveness guard, so a submit that crashed without clearing the
    row does not leave a phantom line. Keyed by `started_at`, which is the same
    `at` the ended history row and the manifest turn will carry, so the line
    merges into one row as the turn progresses rather than doubling.
    """
    import os as _os
    from .state.store import StateStore

    np = StateStore().get_now_playing("speech")
    if not np:
        return None
    ex = np.get("extras")
    if isinstance(ex, str):
        try:
            ex = json.loads(ex)
        except ValueError:
            ex = {}
    if not isinstance(ex, dict):
        return None
    if ex.get("source_session") != session:
        return None
    # A question is spoken on the alert lane but belongs to the conversation —
    # same exception session_feed.turns makes, for the same reason.
    if ex.get("kind") == "notif" and not isinstance(ex.get("ask"), list):
        return None
    wp = ex.get("writer_pid")
    if wp:
        try:
            _os.kill(int(wp), 0)
        except (OSError, ValueError):
            return None
    at, text = np.get("started_at"), (ex.get("text") or "")
    if at is None or not text.strip():
        return None
    # The sentence being spoken, for a transcript that wants to show it: the
    # same `clip_sentences` + `current_sentence_idx` the terminal highlight and
    # the follow pane read, kept up to date by the sentence loop as it plays.
    try:
        sentence = int(ex.get("current_sentence_idx"))
    except (TypeError, ValueError):
        sentence = None
    # The timeline as well as the index, so a reader can move the highlight on
    # its own clock between asks instead of trailing the voice by a poll. The
    # phone lane records where each sentence starts; the local lane records
    # how long each clip is, which is the same thing summed. Elapsed is read
    # the way the sentence loop reads it (intake.submit.elapsed_from_row):
    # from `play_started_at`, frozen at `paused_at` while paused.
    offsets = [float(x) for x in (ex.get("clip_offsets_s") or [])]
    if not offsets and ex.get("clip_starts_s"):
        # When each sentence actually started, measured as the reply plays —
        # truer than summed durations, which count from submit and leave out
        # the gaps between clips. But measured starts only run as far as the
        # sentence playing, and a reader that waits to be told the next one
        # has begun moves a beat behind the voice at every boundary. So the
        # ones still to come are predicted from the last measured start plus
        # the clip lengths in between, and each new measurement corrects them.
        offsets = [float(x) for x in ex["clip_starts_s"]]
        durations = [float(d or 0) for d in (ex.get("clip_durations_s") or [])]
        n = len(ex.get("clip_sentences") or [])
        while offsets and len(offsets) < n and len(offsets) <= len(durations):
            offsets.append(offsets[-1] + durations[len(offsets) - 1])
    if not offsets and ex.get("clip_durations_s"):
        acc = 0.0
        for d in ex.get("clip_durations_s") or []:
            offsets.append(acc)
            acc += float(d or 0)
    base = float(ex.get("play_started_at") or at)
    paused_at = ex.get("paused_at")
    now = float(paused_at) if paused_at else time.time()
    # How long after the clock starts the audio is actually heard — the bridge
    # hop, the far player's start, a Snapcast buffer. The terminal highlight
    # waits this long before moving (`_HighlightScheduler`); a reader that
    # does not is that far ahead of the voice, which is exactly how the app's
    # bold read: a sentence early.
    try:
        from .intake.submit import _playout_delay_s

        delay = _playout_delay_s(str(np.get("target") or ""))
    except Exception:  # noqa: BLE001 — a beat off beats no highlight
        delay = 0.0
    if ex.get("clip_starts_s") and not ex.get("clip_offsets_s"):
        # Measured starts are when the player itself reached each sentence, so
        # the hop to the player is already in them. Taking the playout delay
        # off as well put the bold a beat behind at every boundary.
        delay = 0.0
    return {"at": round(float(at), 3), "text": text,
            "listener": bool(ex.get("listener")),
            "sentences": [str(x) for x in (ex.get("clip_sentences") or [])],
            "sentence": sentence, "offsets": offsets,
            "elapsed": round(max(0.0, now - base), 3),
            "delay": round(delay, 3),
            # Whether the offsets were measured by the far player (sentence
            # marks) or apportioned by character count — the second drifts
            # within a reply, and a reader debugging a bold that runs ahead
            # needs to know which it is looking at.
            "measured": bool(ex.get("sentence_marks")),
            "target": str(np.get("target") or ""),
            "server_time": round(time.time(), 3),
            "paused": bool(paused_at),
            # Set on a replay: which history row is audible again, so the
            # transcript can light up that line rather than add a new one.
            "history_id": int(ex.get("history_id") or 0)}


def conversation_log(session: str, folder: Path, *, target=None) -> list:
    """The conversation as lines you can read. `[{start, end, who, text}]`.

    A chapter title is one sentence, because a table of contents is for finding
    your place. Reading what was actually said needs the whole turn, and the
    whole turn is in speech history — the manifest keeps only what it needs to
    name a file. So this joins the two: the manifest and the server's tracks
    give each turn its position in the item, history gives it its words.

    Read-only, and derived on demand. It is not a second copy of the
    conversation to be kept in step with the audio; it is the same rows,
    rendered.
    """
    turns = _read_manifest(session).get("turns", [])

    # History keyed by the same `at` the manifest recorded, so a turn whose
    # audio has been swept from the cache still has its words here. Speech
    # history is written per turn as it is spoken — well before the debounced
    # publish writes that turn into the manifest — so it is also the source of
    # the *live tail*: turns said but not yet placed in the audio item.
    said = {}
    for t in session_feed.turns(session):
        said[round(float(t.at), 3)] = t

    live = _live_turn(session)
    if not turns and not said and not live:
        return []

    positions = []
    ready = _abs_ready(target)
    if ready and turns:
        url, token, libs = ready
        item = _find_item(url, token, libs, folder)
        if item:
            try:
                import json as _json
                import urllib.request as _u
                req = _u.Request(f"{url}/api/items/{item['id']}?expanded=1",
                                 headers={"Authorization": f"Bearer {token}"})
                with _u.urlopen(req, timeout=20) as r:
                    full = _json.loads(r.read())
                positions = chapters_from(
                    turns, (full.get("media") or {}).get("tracks") or [])
            except Exception as e:  # noqa: BLE001 — a log without times still reads
                log.warning("book-tracks: no track offsets for the log (%s)", e)

    def _line(who_listener, text, pos, at=None, key="", ask=None, command=None, rid=0):
        text = (text or "").strip()
        who = "you" if who_listener else "agent"
        if who == "you" and text.startswith("You: "):
            # The label belongs to the chapter title, where there is no other
            # way to tell the sides apart. Here the side is its own field.
            text = text[len("You: "):]
        # `at` and `key` name the turn for anything that wants to hang more on
        # it — the canvas attaches the picture drawn for a reply by its key.
        line = {"start": pos.get("start"), "end": pos.get("end"),
                "who": who, "text": text, "at": at, "key": key or ""}
        if command:
            line["command"] = command
        if rid:
            # The history row, for `/speech/ctl` replay-id: a tap plays the
            # turn through the speech player rather than the book's.
            line["id"] = rid
        if ask:
            # The spoken sentence is "host / pane: Question. Option 1: …" —
            # right for a voice, wrong for a bubble. Hand over the structure
            # and let the reader lay it out; `text` becomes the question alone
            # so a client that knows nothing of `ask` still reads sensibly.
            line["ask"] = ask
            asked = " ".join(str(q.get("question") or "").strip() for q in ask).strip()
            if asked:
                line["text"] = asked
        return line

    def _said_by_anyone(line) -> bool:
        """Was this line said by a person or the assistant at all?

        The hook used to record the harness's own asides — a finished
        background task, a system reminder — as listener turns, and they are
        still in the history of every conversation from before it stopped.
        They are filtered here as well as at the source so the transcripts
        that already have them read properly rather than staying broken.
        """
        if line.get("who") != "you":
            return True
        from .intake._text import strip_system_blocks
        return bool(strip_system_blocks(line.get("text") or ""))

    out = []
    seen = set()
    for n, turn in enumerate(turns):
        at = round(float(turn.get("at") or 0.0), 3)
        seen.add(at)
        spoken = said.get(at)
        pos = positions[n] if n < len(positions) else {}
        out.append(_line(spoken and spoken.listener,
                         spoken.text if spoken else turn.get("title"), pos,
                         at, spoken.key if spoken else "",
                         ask=(spoken.ask if spoken else None),
                         command=(spoken.command if spoken else None),
                         rid=(getattr(spoken, "id", 0) if spoken else 0)))

    # The live tail: turns in speech history the manifest has not caught up to
    # yet. Shown at once, with no position — the audio item does not place them
    # until the next publish, at which point they join `turns` above and gain
    # their offsets. This is what lets a reply appear in the transcript within
    # a poll of being spoken instead of waiting out the publish cycle.
    for at in sorted(a for a in said if a not in seen):
        seen.add(at)
        out.append(_line(said[at].listener, said[at].text, {}, at,
                         said[at].key, ask=said[at].ask,
                         command=said[at].command, rid=getattr(said[at], "id", 0)))

    # The turn speaking right now, if it has not already landed as an ended
    # row above. This is what puts a reply on screen *while* it is being
    # spoken, not after — keyed by the same `at` it will keep, so it becomes
    # the ended row in place rather than a duplicate.
    replayed = None
    if live and live.get("history_id"):
        replayed = next((l for l in out if l.get("id") == live["history_id"]), None)
    if live and (replayed is not None or live["at"] not in seen):
        line = replayed if replayed is not None else _line(
            live["listener"], live["text"], {}, live["at"])
        # Marked live, with the sentence being spoken, so the transcript can
        # follow the voice sentence by sentence rather than just show the turn.
        # A replay marks the turn it replays, in its place.
        line.update({"live": True, "sentences": live["sentences"],
                     "sentence": live["sentence"], "offsets": live["offsets"],
                     "elapsed": live["elapsed"], "paused": live["paused"],
                     # When `elapsed` was read, so it can be brought up to the
                     # moment the answer leaves (canvas), and the offset the
                     # reader takes off the clock.
                     "server_time": live["server_time"], "delay": live["delay"]})
        if replayed is None:
            out.append(line)
    return [line for line in out if _said_by_anyone(line)]


#: Who a conversation is by. The workspace used to end up here, because it is
#: the folder above and that is where Audiobookshelf looks — which filled the
#: Authors shelf with tmux session names. Override with MEDIA_CONVERSATION_AUTHOR.
CONVERSATION_AUTHOR = "Claude"


def _series_position(session: str, folder: Path) -> str:
    """Where this conversation comes in its workspace, by date. 1-based.

    Recomputed rather than stored: a conversation exported out of order would
    otherwise keep a number that no longer describes it, and renumbering costs
    nothing because the number is only ever read as an ordering.
    """
    workspace = folder.parent.name
    rows = []
    for p in sorted((state_dir() / "book-tracks").glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        other = Path(str(data.get("folder") or ""))
        if other.parent.name != workspace:
            continue
        turns = data.get("turns") or []
        rows.append((float((turns[0].get("at") if turns else 0) or 0),
                     str(data.get("session") or p.stem)))
    rows.sort()
    for n, (_at, sid) in enumerate(rows, 1):
        if sid == session:
            return str(n)
    return ""


LIVE_TAG = "live"


def live_session_ids() -> set[str]:
    """The Claude Code sessions with a pane right now, by uuid.

    The pane registry (`~/.claude/tmux-sessions/<pane>`: uuid, pid, cwd) says
    which session each pane hosts, and is believed only while the pid it
    names is alive and the pane still exists — a recycled pane id would
    otherwise keep a finished session "live" for as long as the file lasts.
    """
    import glob as _glob
    from .state.store import _pid_alive

    try:
        r = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
                           capture_output=True, text=True, timeout=10)
        panes = set(r.stdout.split()) if r.returncode == 0 else set()
    except (OSError, subprocess.SubprocessError):
        return set()
    root = os.path.expanduser(os.environ.get("MEDIA_PANE_REGISTRY_DIR") or "~/.claude/tmux-sessions")
    out: set[str] = set()
    for path in _glob.glob(os.path.join(root, "*")):
        if f"%{os.path.basename(path)}" not in panes:
            continue
        try:
            fields = open(path, encoding="utf-8").read().split()
        except OSError:
            continue
        if not fields:
            continue
        if len(fields) >= 2 and fields[1].isdigit() and not _pid_alive(int(fields[1])):
            continue
        out.add(fields[0])
    # Claude's own record, for a pane whose registry entry went missing.
    from . import claude_sessions

    out.update(sid for pane, sid in claude_sessions.by_pane().items() if pane in panes)
    return out


def _tags_for(session: str, have: list, live: Optional[set] = None) -> list:
    """`have` with the live tag present or absent as the session is live."""
    live = live_session_ids() if live is None else live
    tags = [t for t in (have or []) if t != LIVE_TAG]
    return tags + [LIVE_TAG] if session in live else tags


ARCHIVED_TAG = "archived"


def _archive_after_s() -> float:
    try:
        days = float(os.environ.get("MEDIA_ARCHIVE_AFTER_DAYS") or 7)
    except ValueError:
        days = 7.0
    return days * 86400


def _archived(session: str, have: list, manifest: dict, live: set,
              now: float) -> tuple[list, Optional[float]]:
    """`have` with the archived tag as it should be, and the manifest's new
    `archived_through`.

    `archived_through` is the last turn the conversation had when it was
    archived, and it is what lets the sweep act on edges only, so that it never
    fights a hand in the app:

    * not live, quiet for a week, and not already archived at this turn →
      archive it. Unarchive it by hand and it stays that way, because this
      turn has been archived once.
    * a turn newer than `archived_through` → it came back, so unarchive it.
    * archived by hand (tag, no record) → adopt it at its last turn, so a
      new turn brings it back like any other.
    """
    last = max((float(t.get("at") or 0) for t in manifest.get("turns") or []),
               default=0.0)
    through = manifest.get("archived_through")
    tags = [t for t in (have or []) if t != ARCHIVED_TAG]
    if through is not None and last > float(through):
        return tags, None
    if ARCHIVED_TAG in (have or []):
        return tags + [ARCHIVED_TAG], (last if through is None else through)
    if (through is None and last and session not in live
            and now - last >= _archive_after_s()):
        return tags + [ARCHIVED_TAG], last
    return tags, through


def sync_tags(*, target=None, live: Optional[set] = None,
              now: Optional[float] = None) -> int:
    """Put the live tag on every conversation with a pane and take it off the
    rest, and archive what has been closed and quiet for a week, on every
    server. How many items changed.

    A closed session is not moved out of its series — the series says which
    project it belonged to, which stays true — it just stops being live. The
    app's Live shelf reads the live tag, and hides the archived one.
    """
    servers = _abs_ready_all(target)
    if not servers:
        return 0
    live = live_session_ids() if live is None else live
    now = time.time() if now is None else now
    by_tail: dict[str, tuple[str, dict]] = {}
    for p in (state_dir() / "book-tracks").glob("*.json"):
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        folder = Path(str(data.get("folder") or ""))
        if folder.name:
            by_tail["/".join(folder.parts[-2:])] = (str(data.get("session") or p.stem), data)
    # Every server decides from the record as the sweep found it: the first
    # one's write must not make the second see an archive it has not had.
    before = {tail: dict(m) for tail, (_s, m) in by_tail.items()}
    changed = 0
    for url, token, libs in servers:
        for lib in libs:
            for item in _abs_items(url, token, lib["id"]):
                tail = "/".join(str(item.get("path", "")).replace("\\", "/").split("/")[-2:])
                if tail not in by_tail:
                    continue
                session, manifest = by_tail[tail]
                have = list(((item.get("media") or {}).get("tags")) or [])
                want = _tags_for(session, have, live)
                want, through = _archived(session, want, before[tail], live, now)
                if through != manifest.get("archived_through"):
                    if through is None:
                        manifest.pop("archived_through", None)
                    else:
                        manifest["archived_through"] = through
                    _write_manifest(session, manifest)
                if sorted(want) == sorted(have):
                    continue
                try:
                    _abs_patch(url, token, f"/api/items/{item['id']}/media", {"tags": want})
                    changed += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("book-tracks: could not tag %s on %s (%s)", tail, url, e)
    return changed


def set_metadata(session: str, folder: Path, *, target=None) -> str:
    """Describe the item the way a library should. "" if nothing changed.

    Three fields, and the reason for each:

    * **title** — the conversation, without the workspace. The workspace is the
      folder above, which Audiobookshelf reads as the author, so a title of
      "<workspace> - <question>" said it twice on every shelf.
    * **series** — the workspace, numbered by date. A workspace is an ordered
      set of related items, which is what a series *is*; a collection would
      have to be maintained by hand and would not sort.
    * **author** — Claude, rather than a tmux session name. Deriving the author
      from the folder filled the Authors shelf with things that are not
      authors, and left the field saying nothing true.

    Folders are never renamed for this — a renamed folder is a new item, with
    no progress and the old one stranded — so all of it is metadata, re-applied
    on every publish, which means a rescan that reverts it corrects itself on
    the next turn.
    """
    servers = _abs_ready_all(target)
    if not servers:
        return ""
    turns = session_feed.turns(session)
    title = session_feed.asked_for(session, turns) if turns else ""
    if not title:
        return ""
    workspace = folder.parent.name
    sequence = _series_position(session, folder)
    author = os.environ.get("MEDIA_CONVERSATION_AUTHOR") or CONVERSATION_AUTHOR

    # Fan out: each server is its own item, its own token. One that lacks the
    # item yet, or will not answer, is skipped rather than failing the rest, so
    # a freshly-added second instance catches up on its next scan.
    described = False
    for url, token, libs in servers:
        item = _find_item(url, token, libs, folder)
        if not item:
            continue
        media = (_abs_item(url, token, item["id"]) or {}).get("media") or {}
        md = media.get("metadata") or {}
        have_series = [(x.get("name"), str(x.get("sequence") or ""))
                       for x in (md.get("series") or [])]
        have_authors = [a.get("name") for a in (md.get("authors") or [])]
        have_tags = list(media.get("tags") or [])
        # **tags** — `live` while the session has a pane. Status, not
        # membership: a closed session stays in its series.
        tags = _tags_for(session, have_tags)
        if (md.get("title") == title
                and have_series == [(workspace, sequence)]
                and have_authors == [author]
                and sorted(tags) == sorted(have_tags)):
            described = True
            continue
        try:
            _abs_patch(url, token, f"/api/items/{item['id']}/media",
                       {"tags": tags,
                        "metadata": {
                           "title": title,
                           # Arrays, not the seriesName/authorName fields: those
                           # are the read side of the same thing and a write to
                           # them is accepted and ignored.
                           "series": [{"name": workspace, "sequence": sequence}],
                           "authors": [{"name": author}]}})
            described = True
        except Exception as e:  # noqa: BLE001 — metadata is not worth a failure
            log.warning("book-tracks: could not describe %s on %s (%s)",
                        folder.name, url, e)
    return title if described else ""


def sync_progress(folder: Path, *, target=None) -> Optional[str]:
    """Put a listener's saved progress back in step with the grown item.

    Returns a line worth printing, or None — and None covers every ordinary
    call as well as every failure: no Audiobookshelf configured, no item
    scanned for this folder yet, nobody who has played it, nothing adrift.

    Two things drift when a conversation grows under a listener, and neither
    is something Audiobookshelf fixes on the next scan.

    `isFinished` sticks. Reach the end of what exists, let a turn land, and
    the item stays finished: the new turn is on the server, correctly placed,
    and out of Continue Listening — which is the one place anyone would look
    for it. Clearing it is two calls and the order is not decoration, because
    clearing the flag in the same body as a position RESETS `currentTime` to
    zero: ABS reads un-finishing as starting over.

    The saved `duration` sticks too, and that one shows to a listener who
    finished nothing. ABS records the length the item had when the position
    was written and never revises it, so a conversation paused two sentences
    in reads as two sentences long ever after — a total of 174s against an
    item of 5119s, and a progress bar at 58% of something 2% played (measured
    2026-09-18, "Reply box in Sasonica"). It healed only when the listener
    played it again and a fresh session counted the tracks. So re-baseline it
    here, where we have just made the server look at the new files: same
    position, the length the item is now.
    """
    ready = _abs_ready(target)
    if not ready:
        return None
    url, token, libs = ready
    try:
        item = _find_item(url, token, libs, folder)
        if not item:
            return None
        prog = _abs_progress(url, token, item["id"])
        if prog is None:
            return None

        at = float(prog.get("currentTime") or 0.0)
        was = float(prog.get("duration") or 0.0)
        duration = float(item.get("media", {}).get("duration") or 0.0)
        finished = bool(prog.get("isFinished"))
        # A second is the noise floor of a duration ABS computed itself; only
        # a real append is worth a write, because every write to progress
        # bumps the item to the top of Continue Listening.
        adrift = duration > 0 and abs(duration - was) > 1.0
        if not finished and not adrift:
            return None

        if finished:
            _abs_patch(url, token, f"/api/me/progress/{item['id']}",
                       {"isFinished": False})
        body = {"currentTime": at}
        if duration > 0:
            body["duration"] = duration
            body["progress"] = min(1.0, at / duration)
        _abs_patch(url, token, f"/api/me/progress/{item['id']}", body)
        if finished:
            log.info("book-tracks: reopened %s at %.0fs of %.0fs",
                     folder.name, at, duration)
            return f"re-opened (it had been finished), at {at:.0f}s of {duration:.0f}s"
        log.info("book-tracks: re-based %s at %.0fs: %.0fs -> %.0fs",
                 folder.name, at, was, duration)
        return f"progress re-based at {at:.0f}s: {was:.0f}s -> {duration:.0f}s"
    except Exception as e:  # noqa: BLE001 - a library that will not answer is not this job's problem
        log.warning("book-tracks: progress sync failed (%s)", e)
        return None
