"""`media share`: a URL arrives from elsewhere, and a channel is chosen for it.

The Android share sheet hands us a link and nothing else. Deciding what to do
with it is the whole job, and it is a judgement the URL alone cannot answer: a
two-minute YouTube link and a three-hour one want opposite behaviour from the
speech coordinator. Music **ducks** under a spoken clip; longform **pauses and
rewinds**, so you don't lose the narration you were listening to. Getting that
wrong is not cosmetic — it is the difference between missing a sentence and
missing a paragraph.

So the pipeline is: probe the URL for metadata (`yt-dlp -J`), classify from
that metadata, then dispatch to the channel that fits.

Three deliberate choices:

**The probe runs wherever `media share` runs, which is the phone.** YouTube
bot-blocks red5's datacenter IP (see `sinks/music_fetch`), so a probe from the
hub would fail on exactly the links most worth sharing. On the phone's
residential IP it succeeds, and the acquisition that follows is already
phone-side for the same reason.

**Classification is a pure function.** `classify()` takes a `Probe` and returns
a `Verdict` with no I/O anywhere in it, because heuristics that nobody can test
are heuristics nobody can change. Every rule below is covered in
`tests/test_share.py`; add a rule, add a case.

**Dispatch shells out to the CLI's own subcommands** rather than reaching for
the sinks directly. `media music play` and `media book play` already resolve
targets, content types, phone-vs-rooms routing and the fetch fallbacks; a
second path into the sinks would be a second set of those decisions, drifting
quietly out of step with the first. A share is a typed command the user did not
have to type — nothing more.

What is deliberately NOT here: video. There is no video sink; the canvas shows
figures, not playback. A shared link that carries no audio stream is reported
as such and dropped rather than half-played.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, List, Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# The loopback port the on-device share listener binds. Same trust boundary as
# `mpv-music-bridge-local` and the companion app's `StatusServer`: 127.0.0.1
# and nothing else, on a single-user phone. Never widen it — the payload is a
# "play this" command, so anything that can reach it can drive the speakers.
DEFAULT_PORT = 8771

# Anything at or past this is longform: the book channel, which pauses under
# speech and resumes with a rewind. 30 minutes separates a song or a talk from
# a lecture, an episode or a set with a plot.
LONGFORM_S = 1800.0

# A shorter piece still reads as spoken-word when the metadata says so — a
# 20-minute news explainer is not a track. Below this even a talky category is
# left on music, because ducking a five-minute clip loses nothing.
SPOKEN_S = 600.0

# yt-dlp categories that mean "someone talking" on YouTube. Coarse on purpose:
# the duration gate above does the real work, and this only decides which side
# of the line an ambiguous middle-length item falls.
SPOKEN_CATEGORIES = frozenset({
    "education",
    "news & politics",
    "nonprofits & activism",
    "people & blogs",
    "science & technology",
    "howto & style",
})

# Hosts whose links are spoken-word by construction — no duration needed.
PODCAST_HOSTS = (
    "podcasts.apple.com",
    "pca.st",
    "overcast.fm",
    "castbox.fm",
    "player.fm",
    "podbean.com",
    "buzzsprout.com",
    "libsyn.com",
    "megaphone.fm",
    "audioboom.com",
    "rss.com",
)

AUDIOBOOK_HOSTS = (
    "audible.com",
    "audible.com.au",
    "librivox.org",
    "audiobookshelf.org",
)

# ...and hosts that are music by construction. `music.youtube.com` matters most:
# it is the same video ids as youtube.com but the intent is never a lecture.
MUSIC_HOSTS = (
    "music.youtube.com",
    "soundcloud.com",
    "bandcamp.com",
    "mixcloud.com",
    "beatport.com",
    "tidal.com",
)

_URL_RE = re.compile(r"https?://[^\s<>\"']+")

# A share from an Android app is rarely a bare URL — it is "Title — Artist
# https://youtu.be/xxxx" or a sentence with the link at the end. Take the URL
# and drop the prose; the metadata we want is better than the prose anyway.
_TRAILING_PUNCT = ".,;:!?)]}'\"…"


class ShareError(Exception):
    """The share cannot be acted on, with a reason fit to show the sharer."""


@dataclass
class Probe:
    """What the metadata fetch learned. Every field optional: a probe that
    half-fails still classifies, just with less confidence."""

    url: str
    title: str = ""
    duration_s: Optional[float] = None
    categories: List[str] = field(default_factory=list)
    uploader: str = ""
    extractor: str = ""
    live: bool = False
    has_audio: bool = True
    probed: bool = False  # False when the metadata fetch was skipped or failed

    @property
    def host(self) -> str:
        try:
            # removeprefix, NOT lstrip: lstrip takes a character *set*, so
            # `lstrip("www.")` eats the leading w of wired.com.
            return (urlparse(self.url).hostname or "").lower().removeprefix("www.")
        except ValueError:
            return ""


@dataclass
class Verdict:
    channel: str  # "music" | "book"
    content_type: str  # music | dj-set | ambient | podcast | audiobook
    reason: str  # one human-readable phrase: why this channel
    title: str = ""

    def line(self) -> str:
        """One line for a toast, a log, or a terminal."""
        what = self.title or "link"
        return f"{what} → {self.channel} ({self.content_type}): {self.reason}"


def extract_url(text: str) -> str:
    """Pull the first http(s) URL out of shared text.

    Android's EXTRA_TEXT is whatever the sharing app felt like sending; YouTube
    sends the title and the link together, and some apps append a referral
    tail. Anything with no URL in it at all is not a share we can act on.
    """
    m = _URL_RE.search(text or "")
    if not m:
        raise ShareError("no link in the shared text")
    return m.group(0).rstrip(_TRAILING_PUNCT)


def _host_matches(host: str, needles) -> bool:
    return any(host == n or host.endswith("." + n) for n in needles)


def probe(url: str, *, timeout: float = 30.0,
          runner: Optional[Callable[[List[str], float], str]] = None) -> Probe:
    """Ask yt-dlp what this link is. Never raises: a failed probe is a Probe
    with `probed=False`, which `classify` treats as "URL shape only".

    A share must always end in *something* playing or a clear message. Falling
    over because a metadata fetch timed out would be the worst of both.
    """
    p = Probe(url=url)
    run = runner or _run_ytdlp
    try:
        out = run(["--dump-single-json", "--no-playlist", "--skip-download",
                   "--no-warnings", url], timeout)
    except Exception as e:  # noqa: BLE001 — every failure is the same failure
        log.info("share probe failed for %s (%s); classifying on URL shape", url, e)
        return p
    try:
        meta = json.loads(out)
    except Exception:  # noqa: BLE001
        log.info("share probe returned non-JSON for %s", url)
        return p
    if isinstance(meta, dict) and meta.get("entries"):
        # A playlist that survived --no-playlist (a channel URL, a mix): judge
        # it by its first entry, which is what will actually play first.
        entries = [e for e in meta["entries"] if isinstance(e, dict)]
        meta = entries[0] if entries else meta
    if not isinstance(meta, dict):
        return p
    p.probed = True
    p.title = str(meta.get("title") or "")
    dur = meta.get("duration")
    p.duration_s = float(dur) if isinstance(dur, (int, float)) else None
    cats = meta.get("categories") or []
    p.categories = [str(c) for c in cats if c]
    p.uploader = str(meta.get("uploader") or meta.get("channel") or "")
    p.extractor = str(meta.get("extractor_key") or meta.get("extractor") or "")
    p.live = bool(meta.get("is_live") or meta.get("live_status") == "is_live")
    # `vcodec`-only formats happen; audio-less links (an image post, a plain
    # article) come back with no format at all.
    p.has_audio = bool(meta.get("formats") or meta.get("url")
                       or meta.get("acodec") not in (None, "none"))
    return p


def _run_ytdlp(args: List[str], timeout: float) -> str:
    exe = os.environ.get("MEDIA_YTDLP") or shutil.which("yt-dlp")
    if not exe:
        raise ShareError("yt-dlp not installed")
    r = subprocess.run([exe, *args], capture_output=True, text=True,
                       timeout=timeout, check=True)
    return r.stdout


def _longform_s() -> float:
    try:
        return float(os.environ.get("MEDIA_SHARE_LONGFORM_S", LONGFORM_S))
    except ValueError:
        return LONGFORM_S


def classify(p: Probe, *, channel: str = "", content_type: str = "",
             longform_s: Optional[float] = None) -> Verdict:
    """Pick a channel and an interruption behaviour. Pure — no I/O, no env
    beyond the threshold, so every rule below is a test case.

    Order matters: the explicit override first, then what the host tells us for
    certain, then what the metadata suggests, and length last as the tiebreak.
    """
    limit = _longform_s() if longform_s is None else longform_s
    host = p.host
    cats = {c.strip().lower() for c in p.categories}
    uploader = p.uploader.strip().lower()

    def v(ch: str, ct: str, why: str) -> Verdict:
        # An explicit flag overrides the rule that fired, but the rule's reason
        # is still worth saying — it is how you learn the default was wrong.
        if channel:
            ch = channel
        if content_type:
            ct = content_type
        if channel or content_type:
            why = f"{why} (overridden)"
        return Verdict(channel=ch, content_type=ct, reason=why, title=p.title)

    if not p.has_audio and p.probed:
        raise ShareError("no audio in that link")

    if _host_matches(host, AUDIOBOOK_HOSTS):
        return v("book", "audiobook", f"{host} is an audiobook source")
    if _host_matches(host, PODCAST_HOSTS):
        return v("book", "podcast", f"{host} is a podcast source")
    if _host_matches(host, MUSIC_HOSTS):
        return v("music", "music", f"{host} is a music source")
    if host.endswith("spotify.com"):
        # Spotify episode URLs are podcasts; everything else there is music.
        if "/episode/" in p.url or "/show/" in p.url:
            return v("book", "podcast", "a Spotify episode")
        return v("music", "music", "a Spotify link")

    if p.live:
        # A live stream has no end to rewind to and no place worth saving.
        return v("music", "ambient", "a live stream")

    # A "- Topic" channel is YouTube's auto-generated artist upload; VEVO is
    # the label equivalent. Both are music regardless of length — a 90-minute
    # album upload is still an album.
    if uploader.endswith(" - topic") or uploader.endswith("vevo"):
        return v("music", "music", f"{p.uploader} is an artist channel")
    if "music" in cats:
        dur = p.duration_s or 0.0
        if dur >= limit:
            # Long and categorised music: a DJ set or a full show. Same duck
            # behaviour as music, but named so the coordinator's DJ-set rules
            # (and anything reading the intent later) can tell them apart.
            return v("music", "dj-set", f"music, {_mins(dur)} long")
        return v("music", "music", "categorised as music")

    dur = p.duration_s
    if dur is not None and dur >= limit:
        ct = "podcast" if cats & SPOKEN_CATEGORIES else "audiobook"
        return v("book", ct, f"{_mins(dur)} long")
    if dur is not None and dur >= SPOKEN_S and (cats & SPOKEN_CATEGORIES):
        return v("book", "podcast", f"spoken-word, {_mins(dur)} long")

    if not p.probed:
        # URL shape only. Music is the safe default: the worst case is a clip
        # that ducks when it should have paused, and the sharer can move it.
        return v("music", "music", "no metadata; defaulting to music")
    return v("music", "music", "short-form audio")


def _mins(seconds: float) -> str:
    m = int(seconds // 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m" if m else f"{h}h"
    return f"{m}m"


def dispatch(url: str, verdict: Verdict, *, where: str = "",
             runner: Optional[Callable[[List[str]], int]] = None) -> int:
    """Hand the link to the channel `verdict` chose, via the CLI's own command.

    `where` is the music `--where` value; empty means "whatever this host's
    default is", which on the phone is the phone. The book channel names the
    same places (local / rooms / phone) but has no listener-aware `auto`, so
    those two values become "use the configured default" there.
    """
    if runner is None:
        from .cli import main as runner  # local: cli imports are not cheap
    # The title is worth carrying: a share is the one play path that already
    # knows one, and without it `media recent` lists shared links as video ids.
    title = ["--title", verdict.title] if verdict.title else []
    if verdict.channel == "book":
        argv = ["book", "play", url, *title]
        if where and where not in ("default", "auto"):
            argv += ["--target", where]
        return runner(argv)
    argv = ["music", "play", url, "--as", verdict.content_type, *title]
    if where:
        argv += ["--where", where]
    return runner(argv)


def share(text: str, *, channel: str = "", content_type: str = "",
          where: str = "", probe_timeout: float = 30.0,
          do_probe: bool = True) -> tuple[str, Verdict]:
    """The whole pipeline, minus the dispatch: text in, (url, verdict) out.

    Split from `dispatch` so the listener can answer the phone *fast* — the
    sharer gets the verdict in a toast while acquisition, which for a YouTube
    link means a full download, takes its time on a background thread.
    """
    url = extract_url(text)
    p = probe(url, timeout=probe_timeout) if do_probe else Probe(url=url)
    return url, classify(p, channel=channel, content_type=content_type)
