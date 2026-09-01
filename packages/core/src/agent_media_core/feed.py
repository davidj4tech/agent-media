"""The spool a podcast client subscribes to.

Rendered speech already exists as audio on disk; what it has never had is a
place to live where something other than this codebase can play it. A feed is
that place — and the reason it is a feed rather than a book is that every
phone already ships a client which does queueing, resume position, playback
speed, offline download and lock-screen controls. All of that is work this
repo does not then have to do (see
`docs/proposals/2026-08-30-a-feed-of-what-was-said.md`).

This module is the spool and the XML. It does not render anything, does not
serve anything, and knows nothing about sessions or documents — callers hand
it a finished audio file and a guid, and it takes custody.

## The spool is the database

    $XDG_STATE_HOME/agent-media/feed/<name>/
        <id>.mp3       the episode
        <id>.json      its sidecar: guid, title, description, times, size
        feed.xml       generated from the sidecars

There is no table in `state.db`, on purpose. The audio and the metadata that
describes it are then one thing: a restored backup is a working feed, a
half-copied episode is visible as such, and there is no way for a row to
survive the file it points at — which is exactly the failure the clip cache
already has (`~/.cache/agent-media/audio` holds the only copy of every clip,
and a row whose audio has been swept still lists as replayable).

Custody is the whole point of the spool. `cache_dir()` is a cache: it is
allowed to delete anything, and on 2026-08-28 a disk-full sweep did, taking
373 turns' audio. `state_dir()` is not. Publishing copies rather than links
for that reason alone — a symlink into the cache would inherit the cache's
permission to vanish.

## Idempotent by guid

An episode is identified by a caller-supplied guid — a session id, a document
path, a digest date. Publishing the same guid twice replaces the episode in
place and keeps its position in the feed. Callers therefore do not have to ask
"have I published this already"; they publish, and the answer is the same
either way. The filename is a hash of the guid rather than the guid itself,
because guids are paths and session ids and the filesystem should not have an
opinion about either.

## Two functions that touch the disk, one that doesn't

`feed_xml` is pure: episodes in, XML out. It is the part with fiddly rules
(RFC 2822 dates, byte lengths clients refuse to guess, a token that has to
reach the enclosures) and therefore the part worth testing without a spool.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, asdict
from email.utils import formatdate
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit
from xml.sax.saxutils import escape, quoteattr

from . import config
from ._paths import state_dir

log = logging.getLogger(__name__)


def spool_dir() -> Path:
    """Where episodes live. `MEDIA_FEED_SPOOL` overrides.

    Under `state_dir()`, never `cache_dir()` — see the module docstring.
    """
    raw = os.environ.get("MEDIA_FEED_SPOOL", "").strip()
    return Path(raw).expanduser() if raw else state_dir() / "feed"


# A feed name becomes a directory and a URL path segment, so it is checked
# rather than trusted: `..` in either of those is a way out of the spool.
_SAFE_NAME = "abcdefghijklmnopqrstuvwxyz0123456789-_"


def valid_name(name: str) -> bool:
    n = (name or "").strip().lower()
    return bool(n) and all(c in _SAFE_NAME for c in n)


def feed_dir(name: str) -> Path:
    if not valid_name(name):
        raise ValueError(f"bad feed name: {name!r}")
    return spool_dir() / name.strip().lower()


# --- what a feed is --------------------------------------------------------

#: Per-feed defaults. `keep_days`/`keep_max` of 0 mean "keep everything".
#:
#: These are guesses at intent, not policy: a document you queued is a thing
#: you asked for and should not evaporate before you get to it, a conversation
#: is worth having around for a season, and yesterday's agenda is nothing at
#: all. All three are overridable per host, which is where any real opinion
#: belongs.
@dataclass(frozen=True)
class Policy:
    keep_days: int = 0
    keep_max: int = 0


DEFAULT_POLICIES = {
    "docs": Policy(),                    # keep until removed by hand
    "talks": Policy(keep_days=90),
    "digest": Policy(keep_days=7),
}

#: The shelf-life a client would want and we cannot offer: "keep until
#: played". Play state lives in the subscriber's app and is never reported
#: back, so retention here is time and count only. Anything else would be a
#: guess presented as a fact.


def default_policy(name: str) -> Policy:
    """The built-in policy for a feed nobody has configured.

    `docs` and `digest` are named above. Everything else is a conversation
    feed — one per tmux workspace, created the first time something is
    published from it — and inherits `talks`, because the alternative is a
    directory per project that grows forever and is never pruned by anything.
    """
    n = (name or "").strip().lower()
    if n in DEFAULT_POLICIES:
        return DEFAULT_POLICIES[n]
    return DEFAULT_POLICIES["talks"]


def policy(name: str, path: Optional[Path] = None) -> Policy:
    """Retention for `name`, from `[feeds.<name>]` in config.toml.

    Absent beats wrong, as everywhere else in the config surface: a missing
    table, a malformed value or a negative number falls back to the default
    rather than raising. The failure mode of a bad retention value must be
    "kept too long", never "deleted early" — one is a full disk and the other
    is the recording you wanted.
    """
    table = (config.load(path).get("feeds") or {}).get(name.strip().lower())
    base = default_policy(name)
    if not isinstance(table, dict):
        return base

    def _n(key: str, fallback: int) -> int:
        v = table.get(key, fallback)
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else fallback

    return Policy(keep_days=_n("keep_days", base.keep_days),
                  keep_max=_n("keep_max", base.keep_max))


@dataclass(frozen=True)
class Episode:
    guid: str
    title: str
    filename: str
    published: float
    size: int
    duration_s: float = 0.0
    description: str = ""
    #: Where it came from — a doc path, a session id. Never shown to a client;
    #: it is what lets a later version of the publisher find its own work.
    source: str = ""

    @property
    def eid(self) -> str:
        return self.filename.rsplit(".", 1)[0]


def _eid(guid: str) -> str:
    return hashlib.sha256(guid.encode("utf-8", "replace")).hexdigest()[:16]


# --- publishing ------------------------------------------------------------

def _probe_duration(p: Path) -> float:
    """Seconds, or 0.0. Measured once at publish, stored in the sidecar, so
    that generating the XML never has to shell out."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return max(0.0, float(out))
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def publish(name: str, audio: Path, *, guid: str, title: str,
            description: str = "", published: Optional[float] = None,
            source: str = "", duration_s: Optional[float] = None) -> Episode:
    """Take custody of `audio` and return the episode.

    Copied, not moved and not linked: the caller's file is usually in the
    render cache, which is allowed to delete it, and usually still wanted
    there (a doc played twice should not re-render because it was published
    once).

    Written to a temporary name and renamed into place, both for the audio and
    the sidecar, so a client polling mid-publish sees either the old episode or
    the new one. An enclosure that 404s or truncates is the one failure a
    podcast client handles badly — several cache the broken response.
    """
    audio = Path(audio)
    if not audio.is_file() or audio.stat().st_size == 0:
        raise ValueError(f"no audio to publish: {audio}")
    if not (guid or "").strip():
        raise ValueError("an episode needs a guid")

    d = feed_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    eid = _eid(guid)
    suffix = audio.suffix.lower() or ".mp3"
    dest = d / f"{eid}{suffix}"

    tmp = d / f".{eid}{suffix}.part"
    shutil.copyfile(audio, tmp)
    os.replace(tmp, dest)

    ep = Episode(
        guid=guid,
        title=(title or "").strip() or guid,
        filename=dest.name,
        published=float(published if published is not None else time.time()),
        size=dest.stat().st_size,
        duration_s=(duration_s if duration_s is not None
                    else _probe_duration(dest)),
        description=description or "",
        source=source or "",
    )
    _write_sidecar(d / f"{eid}.json", ep)
    # A stale episode of the same guid in another format leaves its audio
    # behind otherwise — publishing an mp3 over an m4a would serve neither.
    for other in d.glob(f"{eid}.*"):
        if other.name not in (dest.name, f"{eid}.json"):
            _unlink(other)
    return ep


def _write_sidecar(path: Path, ep: Episode) -> None:
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(asdict(ep), indent=1, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _read_sidecar(path: Path) -> Optional[Episode]:
    """One episode, or None for anything unreadable.

    A sidecar that cannot be parsed drops its episode from the listing and
    leaves both files alone. The alternative — raising — would take a whole
    feed off the air over one bad file, and the alternative to that — deleting
    it — would destroy the audio it describes.
    """
    try:
        raw = json.loads(path.read_text())
        ep = Episode(
            guid=str(raw["guid"]), title=str(raw["title"]),
            filename=str(raw["filename"]), published=float(raw["published"]),
            size=int(raw["size"]), duration_s=float(raw.get("duration_s") or 0),
            description=str(raw.get("description") or ""),
            source=str(raw.get("source") or ""),
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
        log.warning("feed: ignoring sidecar %s (%s)", path, e)
        return None
    if "/" in ep.filename or ep.filename.startswith("."):
        log.warning("feed: sidecar %s names a file outside the feed", path)
        return None
    if not (path.parent / ep.filename).is_file():
        log.warning("feed: %s has no audio (%s)", path.name, ep.filename)
        return None
    return ep


def episodes(name: str) -> list[Episode]:
    """Every published episode, newest first."""
    d = feed_dir(name)
    if not d.is_dir():
        return []
    out = [ep for ep in (_read_sidecar(p) for p in sorted(d.glob("*.json")))
           if ep is not None]
    return sorted(out, key=lambda e: (-e.published, e.guid))


def feeds() -> list[str]:
    root = spool_dir()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and valid_name(p.name))


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError as e:
        log.warning("feed: could not remove %s (%s)", p, e)


def remove(name: str, guid: str) -> bool:
    """Unpublish one episode. True if anything went."""
    d = feed_dir(name)
    side = d / f"{_eid(guid)}.json"
    ep = _read_sidecar(side) if side.exists() else None
    gone = False
    if ep is not None:
        _unlink(d / ep.filename)
        gone = True
    if side.exists():
        _unlink(side)
        gone = True
    return gone


def gc(name: str, *, now: Optional[float] = None,
       pol: Optional[Policy] = None) -> list[str]:
    """Apply the retention policy. Returns the guids removed.

    Age first, then count, and the count is applied to what age left behind so
    the two cannot argue. `now` is injectable because a retention test that
    sleeps is a test nobody runs.
    """
    pol = pol if pol is not None else policy(name)
    if pol.keep_days <= 0 and pol.keep_max <= 0:
        return []
    now = time.time() if now is None else now
    eps = episodes(name)                      # newest first
    doomed: list[Episode] = []
    if pol.keep_days > 0:
        cutoff = now - pol.keep_days * 86400
        doomed = [e for e in eps if e.published < cutoff]
        eps = [e for e in eps if e.published >= cutoff]
    if pol.keep_max > 0 and len(eps) > pol.keep_max:
        doomed += eps[pol.keep_max:]
    for e in doomed:
        remove(name, e.guid)
    return [e.guid for e in doomed]


# --- the XML ---------------------------------------------------------------

_NS = ('xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
       'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
       'xmlns:atom="http://www.w3.org/2005/Atom"')

_MIME = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".m4b": "audio/mp4",
         ".ogg": "audio/ogg", ".opus": "audio/opus", ".wav": "audio/wav",
         ".flac": "audio/flac"}


def mime_for(filename: str) -> str:
    return _MIME.get("." + filename.rsplit(".", 1)[-1].lower(), "audio/mpeg")


def hms(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _cdata(text: str) -> str:
    # The one sequence that can close a CDATA section early, split across two
    # sections. Descriptions carry spoken text, and spoken text has contained
    # stranger things than "]]>".
    return "<![CDATA[" + (text or "").replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _url(base: str, path: str, token: str = "") -> str:
    url = base.rstrip("/") + "/" + path.lstrip("/")
    if token:
        url += ("&" if urlsplit(url).query else "?") + "k=" + quote(token, safe="")
    return url


def feed_xml(name: str, eps: list[Episode], *, base_url: str, token: str = "",
             title: str = "", description: str = "", link: str = "",
             author: str = "agent-media", now: Optional[float] = None) -> str:
    """RSS for `eps`. Pure: no disk, no clock unless `now` is omitted.

    Three details are the difference between a feed that works and one that
    silently doesn't:

    - **The token has to reach the enclosures.** A capability URL that
      authorises only the XML gives a client a feed that loads and episodes
      that all 401. Every enclosure is built through `_url`, with the same
      token, for that reason.
    - **`length` is the real byte count.** Clients use it for the download
      progress bar and some refuse an enclosure declaring 0.
    - **Dates are RFC 2822**, not ISO 8601, and a client that cannot parse one
      usually drops the episode rather than complaining.
    """
    now = time.time() if now is None else now
    ftitle = title or f"agent-media: {name}"
    fdesc = description or f"Rendered speech from the {name} channel."
    self_url = _url(base_url, f"feed/{name}.xml", token)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<rss version="2.0" {_NS}>', "<channel>",
           f"<title>{escape(ftitle)}</title>",
           f"<description>{_cdata(fdesc)}</description>",
           f"<link>{escape(link or base_url)}</link>",
           "<language>en</language>",
           # A private spool of one person's conversations. The tag is not a
           # security control — the network is — but every crawler and
           # directory that sees it is told plainly not to list this.
           "<itunes:block>Yes</itunes:block>",
           "<itunes:explicit>false</itunes:explicit>",
           f"<itunes:author>{escape(author)}</itunes:author>",
           f'<atom:link href={quoteattr(self_url)} rel="self" '
           'type="application/rss+xml"/>',
           f"<lastBuildDate>{formatdate(now, usegmt=True)}</lastBuildDate>"]

    for ep in eps:
        enclosure = _url(base_url, f"ep/{name}/{ep.filename}", token)
        out += ["<item>",
                f"<title>{escape(ep.title)}</title>",
                # Not a permalink: the guid is ours (a session id, a path),
                # and a client told otherwise will try to fetch it.
                f'<guid isPermaLink="false">{escape(ep.guid)}</guid>',
                f"<pubDate>{formatdate(ep.published, usegmt=True)}</pubDate>",
                f'<enclosure url={quoteattr(enclosure)} '
                f'length="{ep.size}" type="{mime_for(ep.filename)}"/>']
        if ep.duration_s > 0:
            out.append(f"<itunes:duration>{hms(ep.duration_s)}</itunes:duration>")
        if ep.description:
            out += [f"<description>{_cdata(ep.description)}</description>",
                    f"<content:encoded>{_cdata(ep.description)}</content:encoded>"]
        out.append("</item>")

    out += ["</channel>", "</rss>", ""]
    return "\n".join(out)


def write_feed(name: str, *, base_url: str, token: str = "", **kw) -> Path:
    """Regenerate `<feed>/feed.xml` from the sidecars on disk."""
    d = feed_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    xml = feed_xml(name, episodes(name), base_url=base_url, token=token, **kw)
    path = d / "feed.xml"
    tmp = d / ".feed.xml.part"
    tmp.write_text(xml)
    os.replace(tmp, path)
    return path
