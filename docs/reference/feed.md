# The feed — rendered speech as a podcast

Status: **built**, running on red5 since 2026-08-30. The reasoning behind the
shape is in [the proposal](../proposals/2026-08-30-a-feed-of-what-was-said.md);
this page is the surface.

Everything agent-media speaks is rendered to audio and then, historically,
forgotten: the clips live in `~/.cache/agent-media/audio`, which is a cache and
is allowed to delete them — a disk-full sweep on 2026-08-28 destroyed the audio
behind 373 history rows. The feed is where audio goes to be *kept*, and a
podcast client is how it comes back: queue, resume position, playback speed,
offline download and lock-screen controls, none of which we had to write.

An **episode** is one audio file plus a JSON sidecar. A **feed** is a directory
of them, served as RSS. Three feeds exist by convention:

| feed | one episode is | default retention |
|---|---|---|
| `docs` | a document read aloud | kept until removed |
| `<tmux session>` | a conversation from that workspace | 90 days |
| `talks` | a conversation from no workspace at all | 90 days |
| `digest` | a day's agenda | 7 days |

An episode is titled `<tmux session> · <Claude Code's name for the session>`
— the workspace you would recognise, then the name shown in the resume list,
taken from the transcript's `ai-title` records (the last one: the name is
rewritten as a conversation turns out to be about something else). It falls
back to the opening question, which is what was typed before anybody knew
where the afternoon would go.

**A conversation goes to a feed named for its tmux session** —
`p-agent-media`, `scratch` — so a podcast client's subscription list is your
project list rather than one undifferentiated stream. `talks` is the catch-all
for conversations with no workspace behind them. `--feed <name>` overrides,
and any feed nobody has configured inherits the `talks` retention, because the
alternative is a directory per project that nothing ever prunes.

## Publishing

```sh
media doc play <name> --feed            # a document, instead of playing it
media doc agenda --feed                 # today's digest
media feed session [<session-id>]       # one conversation, chaptered
media feed publish-quiet                # every conversation that has finished
media feed publish <feed> <file> --title … --guid …   # anything else
```

`--feed` on `doc play` publishes *instead of* playing. The render is cached
either way, so playing it afterwards is a second command that costs nothing.

**Every episode has a guid, and publishing the same guid replaces it.** A
document's guid is its path, a conversation's is its session id, the agenda's
is its date. So re-publishing after an edit updates the episode rather than
leaving two versions of one thing in a client, and no caller has to ask whether
it has published this before.

The one consequence worth knowing: **clients never re-fetch a guid they already
have.** Audiobookshelf and AntennaPod both match on it. A conversation
published while still in progress will therefore stay short on the phone
forever, even though the spool is correct. Publish a session when it is done.

## Reading

```sh
media feed list [<feed>]        # feeds, episodes, durations
media feed sessions             # conversations available to publish
media feed xml <feed>           # the RSS, without writing it
media feed write <feed>         # regenerate <feed>/feed.xml
media feed remove <feed> <guid>
media feed gc [<feed>]          # apply retention
```

## Switching it on

```sh
media-setup feed          # token, address, services, subscribe URL
```

Opt-in, and a command of its own rather than part of `media-setup init`,
because this is the part of agent-media that publishes recordings of private
conversations to anything that can reach a URL.

It generates a token if there isn't one, takes the bind address from Tailscale
(`--bind` to override; `0.0.0.0` is refused, not warned about), installs the
three services, and prints the subscribe URLs — including an AntennaPod deep
link, so nobody types a 32-character token on a phone keyboard. Idempotent: run
it again and it tells you the URL again without touching the token, since a new
token silently unsubscribes every client that has the old one.

Until it is run, the three units are **skipped with a reason** rather than
installed idle — their templates declare `requires-env: MEDIA_FEED_BASE_URL`.
`media-setup status` reports the feed too: address, whether it is guarded, and
how many episodes each feed holds.

## Serving

`media-feed` (port 8782) serves `/feed/<name>.xml` and `/ep/<name>/<file>`,
plus `/` — a plain-text list of subscribe URLs, which is the easiest way to get
one onto a phone.

```
MEDIA_FEED_BIND=100.x.y.z         # tailnet address; never 0.0.0.0
MEDIA_FEED_TOKEN=…                # required off loopback; the whole auth story
MEDIA_FEED_BASE_URL=http://host:8782   # optional; see below
MEDIA_FEED_SPOOL=…                # default ~/.local/state/agent-media/feed
```

A podcast client cannot send a header, so authentication is a **capability
URL**: `?k=<token>`, carried into every enclosure the feed lists. (A token that
authorises only the XML gives you a feed that loads and episodes that all 401 —
which reads as a broken server rather than wrong auth.) `X-Agent-Media-Token`
and HTTP Basic also work, for `curl`; enclosures only carry `?k=` when the
request that asked for the feed did.

Binding off loopback without a token is a **startup failure**. The enclosures
are recordings of private conversations; a listener that came up anyway would
be an open archive reporting itself healthy. Tailnet only — never the public
interface.

Enclosure hosts come from the request's own `Host` header, so the address that
reached the feed is the address that reaches the episodes. `MEDIA_FEED_BASE_URL`
overrides that (for a reverse proxy) and is also what the *publishing* commands
use to regenerate `feed.xml` — a host without it still publishes; it just does
not rewrite the served XML.

## The same conversations, as books

Audiobookshelf navigates chapters properly for **books** and badly for podcast
episodes — on the Android app, a conversation's chapter list does not navigate
at all. A conversation is nothing but chapters, so the same episodes are also
laid out as a library ABS can scan:

```
~/conversations/<workspace>/<title>/<title>.mp3      # media feed books
```

Author is the workspace, title is the conversation — the grouping the feeds
already use, in the only vocabulary a book library has. **Hardlinks, not
copies**: one inode with two names, so the library and the spool cannot drift
and deleting either never takes the audio with it. An episode that retention
has pruned takes its folder with it, so the library is a mirror rather than a
scrapbook.

It needs a library of its own (`MEDIA_BOOK_EXPORT_ROOT`, default
`~/conversations`, mounted into the container separately) because a subfolder
of the audiobook root would be swept into that library as well. Point the scan
at it with `ABS_LIBRARY_CONVERSATIONS=Conversations` and
`media abs-scan --target conversations`; the publisher does both on every run.

Feeds remain the delivery mechanism — they are what arrives on its own. The
book library is for navigating what arrived.

## Retention

Per feed, in `~/.config/agent-media/config.toml`:

```toml
[feeds.talks]
keep_days = 90
keep_max  = 0      # 0 = unlimited; age is applied first, then count
```

Anything unset falls back to the table above. "Keep until played" is not
offered: play state lives in the subscriber's app and is never reported back,
so it would be a guess presented as a fact.

## Services

Episode descriptions are HTML, one paragraph per chapter: a client renders
them as HTML, so a list joined by newlines arrives as one unbroken wall of
text with the timestamps buried in it.

Installed by `media-setup feed`, or by role with
`media-setup install-services … --now` once the feed is configured:

| unit | what | when |
|---|---|---|
| `agent-media-feed` | serves the feed | always |
| `agent-media-feed-publish` | publishes finished conversations, then syncs ABS | on silence, + every 30 min |
| `agent-media-abs-sync` | subscribes Audiobookshelf to every feed | hourly (safety net) |
| `agent-media-feed-gc` | applies retention, rewrites the XML | daily |

`MEDIA_FEED_QUIET_MIN` (default 60, set to 10 on red5) is how much silence
marks a conversation finished. Short is the point — an episode that arrives an
hour after you stop talking is a different thing from one waiting when you
pick up the phone — but too short publishes a conversation you were only
pausing, and no client ever re-fetches, so that half is the half you keep.

The publisher chains straight into `media-abs-sync` rather than leaving it to
the hourly timer: three hourly stages (publish, subscribe, download) meant a
conversation could take most of an afternoon to reach the phone.

**It is triggered by silence, not by a clock.** The end of every turn arms a
one-shot for the quiet window and cancels the one before it, so a finished
conversation is built the moment the silence is long enough to mean something
— and nothing wakes up at all while you are talking, or while you are not.
The half-hourly timer is only the safety net for turns that could not arm one:
a reboot mid-conversation, or a host without systemd.

Per-turn publishing is not on offer, and cannot be: no client re-fetches a
guid it already has, so an episode published mid-conversation is frozen at
whatever version reached the client first.

The last two are timer-driven, so their units are *inactive between runs* —
which `media doctor` reports as parked, not down.

`agent-media-feed-publish` exits immediately where `MEDIA_FEED_BASE_URL` is
unset: publishing into a spool nothing can reach is disk spent on an audience
of nobody.

## Subscribing

```sh
curl -s "$MEDIA_FEED_BASE_URL/?k=$MEDIA_FEED_TOKEN"     # prints subscribe URLs
```

**AntennaPod** takes a deep link, which saves typing a 32-character token on a
phone keyboard:

```sh
ssh <phone> "am start -a android.intent.action.VIEW \
  -d 'antennapod-subscribe://host:8782/feed/talks.xml?k=<token>'"
```

**Audiobookshelf** can be kept in step automatically:

```sh
media-abs-sync            # subscribe every feed, fetch what is missing
```

Installed as `agent-media-abs-sync`, hourly, on a host that has both feeds and
an `abs-bridge.env`. It reconciles rather than migrates, so it is safe to run
repeatedly: feeds ABS does not have are subscribed, feeds that have grown are
topped up, and an empty or unreadable one is skipped with a line rather than
ending the run. Feed URLs are compared without their query string, so rotating
the token does not subscribe everything twice.

Doing it by hand the first time, **Audiobookshelf** needs a `podcast`-type
library, and refuses tailnet URLs
until the host is allow-listed — 100.64.0.0/10 is CGNAT space and its SSRF
filter blocks it. The symptom is a bare 404 ("Podcast RSS feed request failed")
with the real reason only in the container log. Add to its unit:

```
Environment=SSRF_REQUEST_FILTER_WHITELIST=<feed host>
```

not `DISABLE_SSRF_REQUEST_FILTER=1`, which turns the filter off for every URL.
ABS also does not backfill — `autoDownloadEpisodes` only catches episodes that
appear *after* subscribing, so existing ones need
`POST /api/podcasts/<id>/download-episodes`. It keeps its own copy of every
episode, so published audio then exists twice on the host.

## What the spool is, and why it is not a table

```
$XDG_STATE_HOME/agent-media/feed/<name>/
    <id>.mp3        the episode
    <id>.json       guid, title, description, times, byte size
    feed.xml        generated (the server does not read it)
```

The audio and the metadata describing it are one thing on disk, so a restored
backup is a working feed and no listing can outlive the file it points at —
which is exactly the failure the clip cache has. `<id>` is a hash of the guid,
because guids are paths and session ids and the filesystem should not have an
opinion about either.

Under `state_dir`, never `cache_dir`, and publishing **copies** rather than
links: a link into the cache would inherit the cache's permission to vanish.

The server generates the XML per request rather than serving the `feed.xml` on
disk, so a client syncing between a publish and a rewrite cannot cache a
listing that is missing the episode it came for.
