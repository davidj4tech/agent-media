"""Artist, album, song and genre for Recently played; author, series and
genre for its books and podcasts.

The Media tab's Show menu groups what was played by these (David, 29 Sep
2026: "add things like album artist, song, genre etc to the show menu" and
"a separate one for books and podcasts, just like threads have different
projects").

Music is YouTube, which says little: a title and a channel. An official
upload's channel is "<Artist> - Topic", and a fan upload's title is often
"<Artist> - <Song>"; either gives an artist, and the iTunes catalogue (free,
no key, ~20 requests a minute) gives the song's album and genre — its broad
genres, Rock and Country, which group better than MusicBrainz's "acoustic
rock". A title with no artist is not searched on ("The Gambler" alone is
anyone's), so a mix or a channel's own video keeps its channel as its artist
and no genre.

Books and podcasts are Audiobookshelf items, matched to the play by file
name. Their genres there are "Audiobook" or YouTube's category, so a book's
comes from the iTunes audiobook of the same title and author.

Lookups are slow and rate-limited, so they never hold up a request: what is
known is answered now, and a background thread fills in a few more for the
next visit. Everything learned is kept in `media-meta.json`.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional

from ._paths import state_dir

_FILE = "media-meta.json"
_UA = "agent-media/1.0 ( https://github.com/davidj4tech/agent-media )"
#: iTunes search allows about twenty requests a minute.
_GAP_S = 3.1
#: Lookups per background pass: a long list fills in over a few visits.
_PER_PASS = 15
#: A key that found nothing is tried again after this long.
_RETRY_S = 7 * 86400

_LOCK = threading.Lock()
_BUSY = threading.Event()


def _load() -> dict:
    try:
        d = json.loads((state_dir() / _FILE).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    try:
        path = state_dir() / _FILE
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(path)
    except OSError:
        pass


def _get(url: str, timeout: float = 10.0) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001 — a name is a nicety
        return None


# --- music -------------------------------------------------------------------

#: Words a title carries that are not the song's name.
_NOISE = re.compile(
    r"\s*[\(\[][^)\]]*(official|video|audio|lyric|visuali[sz]er|remaster|hd|hq|4k|live at|recorded live)[^)\]]*[\)\]]"
    r"|\s*w/\s*lyrics.*$|\s*\|.*$",
    re.I)


def _clean(song: str) -> str:
    return _NOISE.sub("", song).strip(" -–—") or song.strip()


def split_title(title: str, channel: str) -> tuple[Optional[str], str, bool]:
    """`(artist, song, sure)` from a YouTube title and channel. `sure` is
    whether the artist is worth a MusicBrainz search."""
    channel = (channel or "").strip()
    title = (title or "").strip()
    if channel.endswith(" - Topic"):
        return channel[: -len(" - Topic")].strip(), _clean(title), True
    for sep in (" - ", " – ", " — "):
        if sep in title:
            a, s = title.split(sep, 1)
            if a.strip() and s.strip() and len(a) <= 60:
                return a.strip(), _clean(s), True
    return (channel or None), _clean(title), False


#: Albums that are not where a song belongs.
_NOT_ALBUM = re.compile(r"greatest hits|best of|\blive\b|collection|essential|anthology|"
                        r"hits\b|gold\b|playlist|now that's|top \d+|compilation|remix|- ep$|- single$", re.I)


def _itunes(term: str, entity: str) -> list[dict]:
    d = _get("https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": term, "entity": entity, "limit": 10}))
    return (d or {}).get("results") or []


def _same(a: str, b: str) -> bool:
    n = lambda x: re.sub(r"[^a-z0-9]", "", (x or "").lower())  # noqa: E731
    return bool(n(a)) and (n(a) in n(b) or n(b) in n(a))


def _song_lookup(artist: str, song: str) -> tuple[Optional[str], Optional[str]]:
    """`(album, genre)` from the iTunes catalogue: the song by that artist on
    a studio album if there is one, its genre either way."""
    hits = [x for x in _itunes(f"{artist} {song}", "song")
            if _same(x.get("artistName"), artist) and _same(x.get("trackName"), song)]
    if not hits:
        return None, None
    album = next((x for x in hits if not _NOT_ALBUM.search(x.get("collectionName") or "")), None)
    return (album or {}).get("collectionName"), hits[0].get("primaryGenreName")


def _oembed(vid: str) -> tuple[str, str]:
    """YouTube's `(title, channel)` for a video (oEmbed answers a datacenter
    IP, where the video itself does not)."""
    d = _get("https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(
        f"https://www.youtube.com/watch?v={vid}", safe=""), timeout=5) or {}
    return str(d.get("title") or "").strip(), str(d.get("author_name") or "").strip()


def _music_lookup(vid: str, title: str) -> dict:
    t, channel = _oembed(vid) if vid else ("", "")
    artist, song, sure = split_title(t or title, channel)
    out = {"artist": artist, "song": song, "album": None, "genre": None}
    if sure and artist:
        out["album"], out["genre"] = _song_lookup(artist, song)
    return out


# --- books ---------------------------------------------------------------------

def _book_genre(title: str, author: str) -> Optional[str]:
    """The iTunes audiobook's genre: the same book by the same author."""
    t = re.sub(r"\s*\((unabridged|abridged)\)", "", title, flags=re.I).split(":")[0].strip()
    for x in _itunes(f"{author} {t}", "audiobook"):
        if _same(x.get("artistName"), author) and _same((x.get("collectionName") or "").split(":")[0], t):
            return x.get("primaryGenreName")
    return None


#: "…: The Iron Druid Chronicles, Book 6" or "(Series 1)".
_SERIES = re.compile(r":\s*(?:Book \w+ of )?(?:the )?(.+?),?\s+(?:Book|Volume|Vol\.?)\s+\w+", re.I)


#: "Stories from The Iron Druid Chronicles": a run of capitalised words
#: ending in one of these.
_NAMED_SERIES = re.compile(r"(?:(?i:the) )?((?:[A-Z][\w'’]* )+(?:Chronicles|Saga|Trilogy|Cycle|Series))\b")


def series_of(title: str, series: str = "") -> Optional[str]:
    if series:
        return re.sub(r"\s*#[\d.]+$", "", series).strip() or None
    m = _SERIES.search(title or "") or re.match(r"(.+?)\s*#\d+\s*[-–:]", title or "")
    if m:
        s = m.group(1).strip()
    else:
        found = _NAMED_SERIES.findall(title or "")
        if not found:
            return None
        s = found[-1].strip()
    s = re.sub(r"^(?i:the)\s+", "", s)
    return "The " + s if re.search(r"chronicles|saga|trilogy|cycle", s, re.I) else s


# --- the cache and its filler ------------------------------------------------------

def known(key: str) -> Optional[dict]:
    with _LOCK:
        v = _load().get(key)
    return v if isinstance(v, dict) else None


def fill(wanted: list[tuple[str, str, dict]]) -> None:
    """Look up, in the background, the keys in `wanted` not yet known (or
    tried long enough ago). Each is `(key, kind, args)`: kind "music" with
    `vid` (and `title` for a video YouTube will not name), or "book" with
    `title`/`author`."""
    if _BUSY.is_set():
        return
    with _LOCK:
        cache = _load()
    now = time.time()
    todo = [w for w in wanted
            if w[0] not in cache
            or (not cache[w[0]].get("found") and now - float(cache[w[0]].get("at") or 0) > _RETRY_S)]
    if not todo:
        return
    _BUSY.set()
    threading.Thread(target=_fill, args=(todo[:_PER_PASS],), daemon=True).start()


def _fill(todo: list[tuple[str, str, dict]]) -> None:
    try:
        for key, kind, a in todo:
            if kind == "music":
                got = _music_lookup(a.get("vid") or "", a.get("title") or "")
            else:
                got = {"genre": _book_genre(a.get("title") or "", a.get("author") or "")}
            got["found"] = bool(got.get("album") or got.get("genre"))
            got["at"] = time.time()
            with _LOCK:
                cache = _load()
                cache[key] = got
                _save(cache)
            time.sleep(_GAP_S)
    finally:
        _BUSY.clear()
