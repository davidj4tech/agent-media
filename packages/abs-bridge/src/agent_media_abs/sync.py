"""`media-abs-sync` — every feed on this host, subscribed in Audiobookshelf.

Conversations are published into a feed per tmux workspace, so the set of
feeds grows on its own: start work in a new project and a new feed appears
with it. Subscribing to each by hand is the kind of chore that gets done twice
and then stops, and the feed nobody subscribed to is indistinguishable from
the feed nobody made.

So: walk this host's feeds, make sure ABS has a podcast for each, and fetch
the episodes it is missing. Safe to run repeatedly — it is a reconciliation,
not a script someone remembers the state of.

Two things ABS does that this exists to work around:

**It matches feeds by URL, and ours carry a capability token.** A rotated
token would otherwise look like a different podcast and be subscribed twice,
so comparison ignores the query string.

**It does not backfill.** `autoDownloadEpisodes` only catches episodes that
appear *after* subscribing, so everything already published has to be asked
for explicitly — which is also what makes this useful on a feed that has been
quietly filling for a week.

Configuration is the bridge's own (`~/.config/agent-media/abs-bridge.env`)
plus the feed's (`MEDIA_FEED_BASE_URL`, `MEDIA_FEED_TOKEN`).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from urllib.parse import urlsplit

from ._abs import Abs, load_env, log

#: Where ABS keeps the podcasts it downloads, in *its* filesystem. A container
#: path, so it cannot be derived from anything on this side.
DEFAULT_ABS_PODCAST_DIR = "/audiobooks/podcasts"


def feed_key(url: str) -> str:
    """The part of a feed URL that identifies the feed.

    Host and path, without the query: the token is a credential that can be
    rotated, and a rotated token must not read as a new podcast.
    """
    parts = urlsplit(url or "")
    return f"{parts.netloc}{parts.path}".rstrip("/").lower()


def episode_key(ep: dict) -> str:
    """What makes an episode the same episode on both sides."""
    return (ep.get("guid")
            or (ep.get("enclosure") or {}).get("url", "").split("?")[0]
            or ep.get("title", ""))


def missing_episodes(feed_eps: list, have: list) -> list:
    """Episodes in the feed that ABS has not got."""
    known = {episode_key(e) for e in have}
    return [e for e in feed_eps if episode_key(e) not in known]


def _size_of(ep: dict) -> int:
    """The declared byte size of an episode, either side of the wire."""
    enc = ep.get("enclosure") or {}
    if enc.get("length"):
        try:
            return int(enc["length"])
        except (TypeError, ValueError):
            pass
    af = ep.get("audioFile") or {}
    return int((af.get("metadata") or {}).get("size") or af.get("size") or 0)


def _duration_of(ep: dict) -> float:
    """Seconds, from whichever side of the wire this dict came."""
    for value in (ep.get("duration"), (ep.get("audioFile") or {}).get("duration")):
        try:
            if value:
                return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


#: Durations are stated to the second in the feed and to the millisecond by
#: ABS, so they never match exactly.
_DURATION_TOLERANCE_S = 2.0

#: Bytes only decide when nothing else can. ABS rewrites tags on download —
#: adding its own ID3 and cover — so its file is reliably ~1-5KB larger than
#: the feed declares. Comparing sizes exactly would call every episode stale
#: on every run and re-download the library forever, taking the listener's
#: position with it each time.
_SIZE_TOLERANCE = 65536


def stale_episodes(feed_eps: list, have: list) -> list:
    """Episodes ABS has, whose audio is no longer what the feed offers.

    A guid identifies a conversation, not a version of one, and no podcast
    client re-fetches a guid it already has. So a conversation that grew after
    somebody downloaded it leaves the client holding an hour of audio and — as
    soon as anything refreshes the notes — a chapter list describing seventy
    minutes. Clicking a chapter past the end of the file you have cannot seek,
    so it starts from the beginning, which is exactly what a broken player
    looks like.

    Duration is the comparison, not byte size: ABS rewrites tags on download,
    so its copy is reliably a few kilobytes larger than the feed declares, and
    an exact size comparison would call every episode stale on every run —
    re-downloading the whole library forever and discarding the listener's
    position each time. Size is the fallback for a feed that states no
    duration, and then only past a tolerance no tag rewrite could reach.
    """
    ours = {episode_key(e): e for e in feed_eps}
    out = []
    for ep in have:
        theirs = ours.get(episode_key(ep))
        if theirs is None:
            continue
        want_s, got_s = _duration_of(theirs), _duration_of(ep)
        if want_s and got_s:
            if abs(want_s - got_s) > _DURATION_TOLERANCE_S:
                out.append(ep)
            continue
        want_b, got_b = _size_of(theirs), _size_of(ep)
        if want_b and got_b and abs(want_b - got_b) > _SIZE_TOLERANCE:
            out.append(ep)
    return out


def safe_dirname(title: str) -> str:
    return re.sub(r"[^\w .()-]", "_", title).strip() or "feed"


def _feed_urls(base: str, token: str, names: list) -> list:
    q = f"?k={token}" if token else ""
    return [(n, f"{base.rstrip('/')}/feed/{n}.xml{q}") for n in names]


def _podcast_library(abs_api: Abs, folder_path: str, *, create: bool):
    libs = abs_api.req("GET", "/api/libraries").get("libraries", [])
    for lib in libs:
        if lib.get("mediaType") == "podcast":
            return lib
    if not create:
        return None
    log(f"creating podcast library at {folder_path}")
    made = abs_api.req("POST", "/api/libraries", {
        "name": "Spoken (agent-media)",
        "folders": [{"fullPath": folder_path}],
        "mediaType": "podcast",
        "icon": "podcast",
        "provider": "itunes",
    })
    return made.get("library", made)


def sync(abs_api: Abs, feeds: list, *, folder_path: str, dry_run: bool = False,
         create_library: bool = True) -> int:
    """Reconcile `feeds` (name, url) into ABS. Returns the number changed."""
    lib = _podcast_library(abs_api, folder_path, create=create_library and not dry_run)
    if lib is None:
        log("no podcast library in Audiobookshelf, and not creating one")
        return 0
    folder = lib["folders"][0]

    existing = {}
    for row in abs_api.req(
            "GET", f"/api/libraries/{lib['id']}/items?limit=200").get("results", []):
        url = ((row.get("media") or {}).get("metadata") or {}).get("feedUrl")
        if url:
            existing[feed_key(url)] = row["id"]

    changed = 0
    for name, url in feeds:
        try:
            parsed = abs_api.req("POST", "/api/podcasts/feed", {"rssFeed": url})
        except Exception as e:  # noqa: BLE001
            # One unreadable feed must not end the reconciliation: this runs
            # over every workspace, and the newest one is the likeliest to be
            # empty or half-written. ABS answers 404 for both "cannot fetch"
            # and "parsed to nothing", so the message is all there is.
            log(f"{name}: skipped ({e})")
            continue
        podcast = parsed.get("podcast") or {}
        meta, feed_eps = podcast.get("metadata") or {}, podcast.get("episodes") or []
        if not feed_eps:
            # An empty feed is a workspace that has not published yet — a
            # subscription to nothing, which ABS would also refuse to parse.
            log(f"{name}: no episodes yet")
            continue
        item_id = existing.get(feed_key(url))

        if item_id is None:
            if dry_run:
                log(f"would subscribe {name} ({len(feed_eps)} episodes)")
                changed += 1
                continue
            meta["feedUrl"] = url
            meta.setdefault("title", f"agent-media: {name}")
            created = abs_api.req("POST", "/api/podcasts", {
                "path": f"{folder_path.rstrip('/')}/{safe_dirname(meta['title'])}",
                "folderId": folder["id"], "libraryId": lib["id"],
                "media": {"metadata": meta}, "autoDownloadEpisodes": True})
            item_id = created.get("id")
            log(f"subscribed {name} -> {item_id}")
            changed += 1
            have = []
        else:
            item = abs_api.req("GET", f"/api/items/{item_id}")
            have = (item.get("media") or {}).get("episodes") or []
            # Setting it in the create body does not stick; it needs its own
            # PATCH, which is also how an older subscription gets switched on.
            if not (item.get("media") or {}).get("autoDownloadEpisodes"):
                if not dry_run:
                    abs_api.req("PATCH", f"/api/items/{item_id}/media",
                                {"autoDownloadEpisodes": True})
                log(f"{name}: turned on auto-download")
                changed += 1

        # A grown conversation is not a new episode and ABS will never
        # re-fetch it on its own — so drop the copy it has and let the
        # download below bring the current one.
        stale = stale_episodes(feed_eps, have)
        if stale:
            log(f"{name}: replacing {len(stale)} episode(s) whose audio changed")
            if not dry_run:
                for ep in stale:
                    abs_api.req("DELETE",
                                f"/api/podcasts/{item_id}/episode/{ep['id']}?hard=1")
                have = [e for e in have if e not in stale]
            changed += 1

        want = missing_episodes(feed_eps, have)
        if want:
            log(f"{name}: fetching {len(want)} episode(s)")
            if not dry_run:
                abs_api.req("POST", f"/api/podcasts/{item_id}/download-episodes", want)
            changed += 1
    return changed


def prune_missing(abs_api: Abs, library_name: str, *, dry_run: bool = False) -> int:
    """Drop items whose files are gone from a scanned library.

    The book tree is a mirror: rename a conversation — which happens the
    moment Claude Code settles on a better name for it — and the old folder
    goes. Audiobookshelf keeps the item anyway, flagged missing, so the
    library fills with ghosts of every title a conversation ever had. Six of
    them, against three real ones, was what "the titles don't match" turned
    out to mean.

    Only a library we own by name, and only items ABS itself has marked
    missing: this deletes, and it must never be pointed at the audiobooks.
    """
    libs = abs_api.req("GET", "/api/libraries").get("libraries", [])
    lib = next((l for l in libs if l.get("name") == library_name), None)
    if lib is None:
        return 0
    rows = abs_api.req("GET", f"/api/libraries/{lib['id']}/items?limit=500")
    missing = [r for r in rows.get("results", []) if r.get("isMissing")]
    if not missing or dry_run:
        if missing:
            log(f"{library_name}: would drop {len(missing)} missing item(s)")
        return len(missing)
    abs_api.req("DELETE", f"/api/libraries/{lib['id']}/issues")
    log(f"{library_name}: dropped {len(missing)} item(s) whose files are gone")
    return len(missing)


def main(argv=None) -> int:
    load_env()
    ap = argparse.ArgumentParser(prog="media-abs-sync",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("--feed-url", action="append", default=[],
                    help="an extra feed to subscribe (another host's; repeatable)")
    ap.add_argument("--folder", default=os.environ.get(
        "ABS_PODCAST_DIR", DEFAULT_ABS_PODCAST_DIR),
        help="podcast folder as ABS sees it (a container path)")
    ap.add_argument("--book-library",
                    default=os.environ.get("ABS_LIBRARY_CONVERSATIONS", "Conversations"),
                    help="scanned library holding conversations as books; its "
                         "missing items are pruned (they are renames)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])

    abs_api = Abs()
    if not abs_api.token:
        log("ABS_TOKEN not set in ~/.config/agent-media/abs-bridge.env — nothing to do.")
        return 0

    feeds = []
    base = (os.environ.get("MEDIA_FEED_BASE_URL") or "").strip()
    if base:
        try:
            from agent_media_core.feed import feeds as local_feeds
            feeds = _feed_urls(base, (os.environ.get("MEDIA_FEED_TOKEN") or "").strip(),
                               local_feeds())
        except Exception as e:  # noqa: BLE001
            log(f"cannot list local feeds: {e!r}")
    for url in a.feed_url:
        feeds.append((urlsplit(url).path.rsplit("/", 1)[-1].removesuffix(".xml"), url))

    if not feeds:
        log("no feeds to sync (set MEDIA_FEED_BASE_URL, or pass --feed-url)")
        return 0
    log(f"syncing {len(feeds)} feed(s): {', '.join(n for n, _ in feeds)}")
    changed = sync(abs_api, feeds, folder_path=a.folder, dry_run=a.dry_run)
    changed += prune_missing(abs_api, a.book_library, dry_run=a.dry_run)
    log("nothing to do" if not changed else f"{changed} change(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
