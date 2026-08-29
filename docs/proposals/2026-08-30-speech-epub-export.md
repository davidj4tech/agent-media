# A conversation, on the shelf: EPUB export for the speech channel

**Status:** not doing, 2026-08-30. Superseded by
`2026-08-30-a-feed-of-what-was-said.md`, which reads the same rows and clips
and lands them somewhere every phone can already play. Kept for §1 (what a
speech turn already records) and §2 (the cache is the only copy), which the
feed proposal builds on.

## Why not Calibre as *the* interface

The question that started this was whether Calibre could front the speech
channel. It can't, and the reason is a shape mismatch rather than a missing
feature. Calibre is a shelf: a metadata DB over documents you pick up, put
down, and come back to. The speech channel is a stream — short, append-only,
strictly ordered — and everything anyone actually does with it (replay the
last turn, step back three, seek inside a sentence, barge in) is a stream
operation. `calibredb` has no verb for any of them, and the viewer has no
notion of "resume this utterance at 40s, on the phone".

What Calibre *is* good at is the thing the speech channel currently does not
do at all: keep a conversation you can come back to. So it isn't the
interface. It's an export target — and, as it happens, the one place the
archive would survive.

## 1. What's already there (this is the whole argument)

Nothing new has to be recorded. A finished speech turn is already written to
`history` with everything a book needs (`intake/submit.py:3133`):

| field | what it is |
|---|---|
| `text` | the full spoken text of the turn — markers stripped, as heard |
| `uri` | the first clip's path |
| `started_at` / `ended_at` | the turn's wall clock |
| `extras.source_session` | the Claude session id — the true conversation boundary |
| `extras.clip_uris` | every rendered clip, in order |
| `extras.clip_sentences` | the sentence each clip speaks |
| `extras.clip_durations_s` | how long each one runs |
| `extras.clip_paragraph_idx` | which paragraph each clip belongs to |
| `extras.visual` | the `[[visual:]]`/`[[reveal:]]` description, when the turn had a figure |

That is a sentence-aligned audiobook with its own text, already on disk. The
exporter is a *reshaping* job, not an instrumentation one.

Two things it does not have, and where they come from:

- **The user's turns.** History only holds what was said aloud. The prompts
  live in the session transcript, which `conversation.transcript(session)`
  already locates by session id (`conversation.py:100`).
- **The figures.** `extras.visual` is the description, not the rendered image.
  The image is in the visual spool (`agent_media_visual.state.spool_dir()`).
  Linking a spool file back to a turn is the one genuinely unsolved bit — see
  §7.

## 2. Preservation is the real payoff

`~/.cache/agent-media/audio` is the only copy of every rendered clip, and it
is a *cache*: the 2026-08-28 disk-full sweep took 373 history rows' audio with
it and left the rows pointing at nothing. The visual spool is worse — it gc's
to the newest 200 images by design.

So an export is not merely a nicer way to read old conversations. It is the
act of moving a turn out of two directories that are explicitly allowed to
delete it, into a single file with a checksum and a backup. That reframes the
priority: this is worth doing even if nobody ever opens the EPUB.

## 3. One session, one book

- **Book** = one `source_session`. It is the only boundary that is neither
  too coarse (a tmux session holds several conversations) nor too fine.
- **Chapter** = one turn, user prompt followed by the spoken reply, titled
  with its timestamp and first sentence. Hundreds of short chapters is fine —
  it is exactly what a TOC is for, and it makes "the bit where we fixed the
  ringer" findable.
- **Untagged rows are excluded**, the same rule `_speech_history` already
  applies when scoping (`cli.py:396`): a row with no `source_session`
  belongs to no conversation, and guessing leaks one conversation into
  another's book.

## 4. Read-along, because the data is already aligned

`clip_uris` + `clip_sentences` + `clip_durations_s` is precisely an EPUB 3
Media Overlay. Each clip becomes one `<par>`:

```xml
<par>
  <text src="ch012.xhtml#s3"/>
  <audio src="../audio/…--claude-code--003.mp3"
         clipBegin="0s" clipEnd="7.4s"/>
</par>
```

Offsets are cumulative sums of `clip_durations_s`; since each clip is its own
file, every `clipBegin` is 0 and `clipEnd` is that clip's duration. No
forced-alignment pass, no whisper, no guessing — the renderer already told us.

A reader that supports overlays (Thorium, Menestrello) plays the conversation
back with the words highlighting. One that doesn't shows plain text and
ignores the audio, which is the correct degradation. **The Pine Note is the
second case** — DU4, no overlay support — and that is fine: on the Note this
is a readable transcript, and read-along is a bonus elsewhere.

Overlays make the EPUB big (an hour of speech is tens of MB). So:
`--audio=none|linked|embedded`, default `embedded` for the archive copy and
`none` for anything pushed to the Note.

## 5. Idempotent, because it will run more than once

A session gets exported while it is still live (you want today's conversation
on the Note tonight) and again when it ends. Re-export must *replace*, never
duplicate:

- EPUB `dc:identifier` = `urn:agent-media:session:<session-id>`.
- Into Calibre with `--identifier session:<id>`; the exporter searches
  `identifiers:session:<id>` first and does `calibredb set_metadata` +
  `add_format` on a hit rather than `add`.
- Turns are appended in `started_at` order, so a re-export of a longer
  session is a superset. Never rewrite a chapter that already shipped.

## 6. Shape of it

A new module, `agent_media_core/export_epub.py` — no dependency on Calibre,
which is one sink among several:

```python
@dataclass(frozen=True)
class Turn:
    at: float
    prompt: str            # from the transcript; "" if unmatched
    spoken: str            # history.text
    clips: list[Clip]      # path, sentence, duration
    visual: str = ""

def turns(session: str, *, since: float = 0.0) -> list[Turn]:
    """History rows for one session, joined to the transcript's prompts."""

def build(session: str, out: Path, *, audio: str = "embedded") -> Path:
    """Write the EPUB. Pure function of the store + the cache."""
```

CLI, beside the existing speech verbs:

```
media speech export [--session ID] [--since 7d] [--out DIR]
                    [--audio none|linked|embedded] [--calibre]
```

`--calibre` shells out to `calibredb add --automerge=overwrite` against
`MEDIA_CALIBRE_LIBRARY`, or POSTs to a content server if
`MEDIA_CALIBRE_URL` is set. Absent both, it just leaves the file in `--out`.
The default session is the one that owns the current pane — `conversation`
already answers that.

Metadata: title = the session's first prompt, trimmed to a line (with a
`--title` override); author = the speaking voice; series = the project
directory's basename, series index = the session's ordinal within it, so a
project's conversations sort in order on the shelf; pubdate = first turn;
tags = project, host.

**Writing an EPUB needs no library.** It is a zip with a mimetype stored
first, a container.xml, an OPF, XHTML per chapter, and (here) SMIL. `zipfile`
plus string templates, and no new dependency for a repo that has been
careful about them.

## 7. The unresolved bit: which picture went with which turn

`extras.visual` gives the *description*; the spool has files. To put the
figure in the chapter, a turn needs the filename. Options, cheapest first:

1. The push payload the canvas already stores (`visual/state.save_push`) is
   keyed per session — if it retains the image path and a timestamp, matching
   to the nearest turn start is a query, not a change.
2. Otherwise: have the visual lane write `extras.visual_path` back onto the
   history row when the image lands, the same after-the-fact fill-in
   `set_history_title` does for book titles.

Ship §1–§6 without figures; a chapter with `extras.visual` but no file gets
the description as an italic caption, which is honest and still useful.

## 8. When it runs

By hand first. Once it has proven itself, a `SessionEnd` hook is tempting but
wrong — it would put a multi-megabyte zip on the critical path of closing a
terminal. A timer that exports sessions with no turn in the last hour, and
nothing since their last export, has the same effect and no latency.

**Never on the speech path.** Nothing in this document may run inside
`submit_event`. An archive that can delay a reply is worse than no archive.
