# A feed of what was said

**Status:** implemented 2026-08-30, and running on red5 — `media-feed` on
8782 (tailnet only, capability URL), `media doc play --feed`,
`media feed session`. §7's four steps are all landed; retention (§5) is
implemented as `media feed gc` but nothing calls it on a timer yet.

Supersedes
`2026-08-30-speech-epub-export.md`, which is not being built — that document's
§1 (what a speech turn already records) and §2 (the cache is the only copy)
are the ground this stands on, and its EPUB output is dropped.

## Why a feed beats a book

Both proposals read the same rows and the same clips. The difference is what
has to exist on the other end.

An EPUB with media overlays needs a reader that supports overlays — Thorium on
a desktop, nothing on the Note. A podcast feed needs a podcast client, and
every phone already has one, offering: a queue, per-episode resume position,
variable speed, offline download, lock-screen and earbud controls, and sleep
timers. That is most of the phone-side surface this repo has been hand-building
for a year, already written, already debugged.

The output is also smaller. An EPUB is a zip you assemble — mimetype ordering,
OPF, XHTML per chapter, SMIL. A feed is an XML document with an `<item>` per
episode and an `<enclosure>` per audio file.

**And a podcast client obeys audio focus.** The companion app exists because
mpv doesn't (see `docs/proposals/2026-08-13-android-companion-app.md`). A
normal Android media app pauses for a call, ducks for a notification, and
yields when Live claims the mic — for free, with no bridge, no loopback
service, and no `sv restart`. Audio that arrives by feed is the best-behaved
audio on the phone.

## 1. Start with the docs channel, because it is already done

`render_doc` (`docs.py:672`) writes a single mp3 with FFMETADATA chapters per
heading, into `~/.cache/agent-media/docs`. That file *is* an episode. Chapter
marks and all — podcast clients render chapters natively.

So v1 is not a rendering project. It is:

1. copy the rendered mp3 out of the cache into a spool that is not garbage
   collected,
2. append a row to a small SQLite table (or a JSON sidecar),
3. regenerate `feed.xml`,
4. serve it.

`media doc play --feed <name>` — render as now, publish instead of (or as well
as) playing. A doc queued at breakfast is on the phone by the time you leave.

## 2. Then conversations

One session, one episode — the same boundary the EPUB export uses, and for the
same reason (`extras.source_session` is the only honest one). A turn is thirty
seconds; nobody subscribes to that.

The clips for a session are already listed per turn in `extras.clip_uris`.
Concatenate them in `started_at` order, and write **one chapter per turn**
using `_write_chapter_metadata`, which exists and does exactly this
(`docs.py:661`). The chapter title is the turn's first sentence. The result is
a browsable conversation: skip to the bit where you asked about the ringer.

`ffmpeg -f concat` over mp3s of one engine and rate is a stream copy — seconds,
not synthesis. Gaps between turns are dropped, which is right; the silence
where you were typing is not content.

The description field carries the text: `history.text` per turn, in a
`<content:encoded>` block with the chapter offsets. Clients show it; it makes
the episode searchable in the client's own search.

## 3. Feeds, plural

`/feed/<name>.xml`, one per kind, because they want different treatment:

| feed | episode | typical use |
|---|---|---|
| `docs` | one document | queued reading, keep until played |
| `talks` | one conversation | catch up on a session away from the desk |
| `digest` | one daily agenda | ephemeral, expire after a week |

A client subscribes to what it wants. Per-project talk feeds
(`/feed/talks-agent-media.xml`) fall out of the same query if they turn out to
matter.

## 4. Serving it

The precedent is the canvas server (`packages/visual/canvas.py`): stdlib
`ThreadingHTTPServer`, tailnet-bound, serving files from a spool at `/img/`,
with a one-time `/pair?c=<code>` flow so no 40-character token is typed on a
phone keyboard. The feed is the same shape: `/feed/<name>.xml` and
`/ep/<id>.mp3`.

**Auth has to change form, though.** A podcast client cannot send an
`Authorization` header. The workable options are a capability URL
(`/feed/talks.xml?k=<token>`, the token carried into every enclosure URL) or
HTTP Basic, which clients do support. Given the server is tailnet-only, a
capability URL is enough — but it must be *in the enclosure URLs too*, or every
episode 401s while the feed itself loads. That is the classic way this breaks.

Never bind this to the public interface. The enclosures are recordings of
private conversations, and a feed is a list of direct links to all of them.

## 5. Retention, and the point of the exercise

Episodes live in a spool with a per-feed policy: `docs` keeps until played,
`talks` keeps 90 days, `digest` keeps 7. The spool is backed up; the cache
is not.

Which is the same argument as the EPUB proposal §2, and worth restating,
because it is the real reason to build either: `~/.cache/agent-media/audio` is
the only copy of every rendered clip, it fills up on its own as you talk, and the
2026-08-28 disk-full sweep already destroyed 373 turns' audio. Publishing an
episode moves it somewhere that is allowed to keep it.

## 6. What this does not do

It is not the live channel. A feed is minutes-to-hours behind, always. Replay,
barge-in, ducking and "say that again" stay exactly where they are.

It does not replace the popup's history traversal, which is for *this*
conversation, now. The feed is for conversations you have finished having.

## 7. Order of work

1. Spool + retention + `feed.xml` generation (pure function of a table).
2. Serve it beside the canvas, capability URL through to the enclosures.
3. `media doc play --feed` — the docs case, which needs no new rendering.
4. Session episodes: concat + per-turn chapters.
