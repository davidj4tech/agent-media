"""What was played, newest first, and which conversation put it on.

The Media tab's Recently played (David, 28 Sep 2026: "it would be handy to
be able to traverse back through the threads in played order" — a Chris
Cornell album started from another thread, and no way to find which).

`history` holds two kinds of music row. One per thing someone asked for,
written by `StateStore.note_play` (a `yt:` or web URI, the title when known,
and since 28 Sep 2026 `extras.source_session`); and one per file a player
loaded, written by `set_now_playing` (a phone path or a loopback URL, with
`extras.interruption`). Only the first kind is a thing to go back to.

A row from before `source_session` was recorded has its conversation
inferred: the newest speech row with a session in the few minutes before
the play — an agent says what it is putting on, or is asked to, just before
it does.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from ._paths import state_dir
from .state import StateStore

#: How far back from a play its conversation's last speech may be.
_INFER_S = 300.0


def _asked(row: dict) -> bool:
    uri = row.get("uri") or ""
    extras = row.get("extras") if isinstance(row.get("extras"), dict) else {}
    if "interruption" in extras:
        return False
    return not uri.startswith(("/", "http://localhost", "http://127.0.0.1", "file:"))


def _vid(uri: str) -> Optional[str]:
    from .sinks import music_fetch

    return music_fetch.watch_id(uri[3:] if uri.startswith("yt:") else uri)


def art(uri_or_id: str) -> Optional[str]:
    """A picture for a YouTube track: its thumbnail on YouTube's image host
    (public, and reachable from anywhere the video itself is not)."""
    if not uri_or_id:
        return None
    vid = _vid(uri_or_id)
    if not vid and len(uri_or_id) == 11 and all(c.isalnum() or c in "-_" for c in uri_or_id):
        vid = uri_or_id
    return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else None


def _key(uri: str) -> str:
    """One entry per video, whether it was asked for as `yt:` or a link."""
    return _vid(uri) or uri


_TITLES = "music-titles.json"
#: oEmbed lookups per call: a long list without names fills in over a few
#: visits rather than holding up one.
_LOOKUPS = 6


def _titles() -> dict:
    try:
        d = json.loads((state_dir() / _TITLES).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _keep_titles(d: dict) -> None:
    try:
        path = state_dir() / _TITLES
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(path)
    except OSError:
        pass


def _oembed(vid: str) -> str:
    """YouTube's public name for a video (oEmbed answers a datacenter IP,
    where the video itself does not)."""
    url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(
        f"https://www.youtube.com/watch?v={vid}", safe="")
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            d = json.loads(r.read().decode())
    except Exception:  # noqa: BLE001 — a name is a nicety
        return ""
    title, who = str(d.get("title") or "").strip(), str(d.get("author_name") or "").strip()
    return f"{title} — {who}" if title and who and who.lower() not in title.lower() else title


def _cached_title(vid: str) -> str:
    """The name the rooms cache learned for a YouTube id, if it has one."""
    from .sinks import music_fetch

    try:
        return (Path(os.path.expanduser("~")) / music_fetch.cache_dir() / f"{vid}.title").read_text().strip()
    except OSError:
        return ""


def _speakers(store: StateStore, since: float) -> list[tuple[float, str]]:
    """`(at, session)` of speech rows since `since`, oldest first."""
    out = []
    for r in store.recent_history(sink="speech", limit=2000):
        if (r.get("started_at") or 0) < since:
            continue
        extras = r.get("extras") if isinstance(r.get("extras"), dict) else {}
        sid = extras.get("source_session")
        if sid:
            out.append((float(r["started_at"]), str(sid)))
    out.sort()
    return out


def _inferred(speakers: list[tuple[float, str]], at: float) -> Optional[str]:
    best = None
    for t, sid in speakers:
        if t > at:
            break
        if at - t <= _INFER_S:
            best = sid
    return best


def recent(limit: int = 40, store: Optional[StateStore] = None) -> list[dict]:
    """``[{"id", "uri", "title", "art", "at", "session", "inferred"}]``, newest
    first, one row per URI (its latest play)."""
    store = store or StateStore()
    rows = [r for r in store.recent_history(sink="music", limit=400, requested=True)
            if _asked(r)]
    # A name any row learned for a video names all its rows.
    named: dict[str, str] = {}
    for r in rows:
        if (r.get("text") or "").strip():
            named.setdefault(_key(r["uri"]), r["text"].strip())
    seen: set[str] = set()
    picked = []
    for r in rows:
        k = _key(r["uri"])
        if k in seen:
            continue
        seen.add(k)
        picked.append(r)
        if len(picked) >= limit:
            break
    cache = _titles()
    looked = 0
    for r in picked:
        k = _key(r["uri"])
        vid = _vid(r["uri"])
        if named.get(k) or not vid:
            continue
        if k in cache:
            named[k] = cache[k]
            continue
        title = _cached_title(vid)
        if not title and looked < _LOOKUPS:
            looked += 1
            title = _oembed(vid)
        if title:
            named[k] = cache[k] = title
    if looked:
        _keep_titles(cache)
    oldest = min((r["started_at"] for r in picked), default=0.0)
    speakers = _speakers(store, oldest - _INFER_S) if picked else []
    out = []
    for r in picked:
        extras = r.get("extras") if isinstance(r.get("extras"), dict) else {}
        sid = extras.get("source_session")
        inferred = False
        if not sid:
            sid = _inferred(speakers, float(r["started_at"]))
            inferred = sid is not None
        out.append({"id": r["id"], "uri": r["uri"],
                    "title": named.get(_key(r["uri"])) or None, "art": art(r["uri"]),
                    "at": r["started_at"], "session": sid or None, "inferred": inferred})
    return out
