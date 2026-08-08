# Proposal: a place for documents, and a way to hear them

Status: **proposed** — nothing moved or built yet
Date: 2026-08-09

Two asks from the same conversation, which turn out to be one design:

1. If detail moves out of spoken replies and into files, those files need a
   system — otherwise "write it down" just means "lose it somewhere else".
2. Those files should be playable from the popup.

The second constrains the first. A document you intend to *listen to* has
requirements a document you only read does not: it needs a stable identity, a
length you can navigate, and a position you can come back to.

## Why now

The repo already has an embryonic convention that never got a home:

```
CHANNELS.md  DECISION-speech-state-convergence.md  NOTES-speech-controls.md
PROPOSAL-play-video.md  RESTRUCTURE.md  README.md  HANDOVER.md
docs/  (9 topic docs, no index)
```

`DECISION-` / `NOTES-` / `PROPOSAL-` is a real taxonomy — it just lives as
filename prefixes at the top level. And the file that rots fastest,
`HANDOVER.md`, is the one that is **gitignored**, so no version of it has ever
survived a session. That is backwards: the most perishable document is the one
with no history.

## The system: organise by lifecycle, not by topic

Topic answers "what is this about", which the title already does. Lifecycle
answers the question that actually gets asked: **when may I delete this?**

```
docs/
  README.md          index — the only entry point, lists everything with status
  reference/         how things are now. Lives as long as the code does.
  decisions/         why we chose X. Dated, append-only; superseded, never edited.
  proposals/         wanted, not built. Ends as a decision + code, or deleted.
  notes/             working knowledge, findings, gotchas. Trimmed freely.
  handover/          session state. Ephemeral by design — but tracked.
```

Naming: `YYYY-MM-DD-slug.md` for decisions, proposals and notes, so staleness
is visible without opening the file; bare `slug.md` for reference, which is
meant to be current or fixed.

Each file opens with three lines — title, status, date — which is what lets the
index and the chooser below be generated rather than maintained.

Moves (nothing deleted):

| from | to |
|---|---|
| `DECISION-speech-state-convergence.md` | `docs/decisions/2026-07-…-speech-state-convergence.md` |
| `PROPOSAL-play-video.md` | `docs/proposals/2026-08-07-play-video.md` |
| `NOTES-speech-controls.md` | `docs/notes/2026-08-…-speech-controls.md` |
| `RESTRUCTURE.md`, `CHANNELS.md` | `docs/reference/` |
| `HANDOVER.md` | `docs/handover/YYYY-MM-DD.md`, **tracked** |
| `docs/*.md` (9 files) | `docs/reference/` |

`README.md` stays at the top level; it is the repo's front door, not a doc.

## Hearing them: a document is a short audiobook

The instinct is a new "docs channel". It shouldn't be. The **book channel
already has everything a long spoken document needs** and nothing it doesn't:
chapters, resume position, bookmarks, speed, and the popup's chapter browser.

A document is a short audiobook. So render it and hand it to that channel:

- **Headings become chapters.** `##` in the markdown becomes an mpv chapter
  mark, so the popup's existing chapter browser navigates the document by
  section. This is the piece that makes a 10-minute file usable — nobody
  listens to a design doc front to back.
- **Resume position for free** (`resume_pos` already exists per URI), so
  stopping halfway through and coming back later works like any book.
- **Sentence/paragraph skip, speed, pause** all already work, because it is
  the same sink.

New surface is small:

```
media doc list                 # the index, as data
media doc play <slug>          # render if needed, play on the book channel
```

and a popup key (`d`) opening a chooser in the style of the existing clip
browser (`^a`), listing title + status + date from those three header lines.

## The part that needs care: markdown is not speakable

Read aloud verbatim, a doc like this one is unbearable — code blocks, tables,
file paths, URLs and punctuation-heavy prose all degrade badly. This needs a
deliberate **speakable projection**, not a naive strip:

- code blocks → "a code example follows, N lines" (skipped, not read)
- tables → skipped, announced by caption
- links → the link text, never the URL
- paths/identifiers → kept, they are usually the point
- headings → spoken, and used as the chapter mark

Rendering is cached per (file, mtime), so a doc is only synthesised once.

## Cost and risk

The organisation half is a directory move plus an index: cheap, reversible,
and it fixes the tracked-vs-ignored inversion on its own. The playback half is
a real feature — chaptered rendering and the speakable projection are the work;
everything downstream of it already exists.

Worth doing in that order, since the first half is useful even if the second
is never built.

## Open questions

- Should `handover/` accumulate one file per session, or one rolling file with
  history? Accumulating is honest but noisy.
- Does the chooser list `reference/` too, or only the dated kinds? Reference
  docs are the ones most worth hearing and the least worth browsing.
- Does a doc get re-rendered on edit automatically, or on demand?
