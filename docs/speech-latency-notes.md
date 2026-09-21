# Time-to-first-audio (21 Sep 2026)

Measured on red5 with `render_text`, the real path, warm-up excluded.
Script: scratchpad `piper_ttfa.py` (sequential vs parallel, per engine).

`submit.py` Phase 1 resolves EVERY render future before any audio plays, so
TTFA today is the SLOWEST sentence, not the first.

| | sentences | 1st clip | last clip (= TTFA today) |
| --- | --- | --- | --- |
| edge | 8 | 1.02s | 1.72s |
| edge | 32 | 2.32s | 4.20s |
| piper | 8 | 0.91s | 1.45s |

Rendered one at a time, a single sentence takes ~0.6s on edge and ~0.21s on
piper. Both numbers are far below the "1st clip" column above, because
`max_workers = len(sentences)` puts every sentence in flight at once and they
contend — 32 concurrent edge sessions, or N POSTs to one local piper server
(which only reaches 1.7x overlap on 8, so it partly serialises).

So the win has two halves, and the second is the bigger one:

1. **Play clip 1 when it is ready**, not when clip N is. Saves 1.9s on a long
   reply.
2. **Give sentence 1 a head start** — render it alone, then the rest in
   parallel. That is what takes TTFA from 2.3s to ~0.6s.

Together: **~4.2s → ~0.6s** on a 32-sentence reply.

## What it costs

Phase 1 also computes `clip_durations_s` and the cumulative offsets the
spanning progress bar and the follow-along clock are built on
(`play_started_at + clip_starts_s`). Streaming means those arrive
incrementally. It works — sentence k's offset needs only durations 1..k-1, and
those are known by the time k plays — but it is surgery on the clock that was
fixed on 19 Sep.

Chunk-level streaming (edge's websocket hands over audio mid-sentence) is a
different thing and not worth it here: it costs the file, the duration is
unknown until the stream ends, and `--write-subtitles` word timings come free
in the CLI render today.

## What was built (21 Sep)

- `_render_workers()` — the pool is bounded, `MEDIA_RENDER_WORKERS=4` by
  default, submitted in order so the early sentences land first. `=0` restores
  one worker per sentence.
- `SinkSpeech.append_clips()` — appends to a playlist that is already playing.
- `MEDIA_STREAM_CLIPS=1` (off by default) — Phase 1 waits only for a lead of
  `MEDIA_STREAM_LEAD_S` (default 6s) of contiguous audio, starts, and a
  background thread hands the rest over as each lands. Remote-playlist targets
  only; the per-sentence local path would have to learn to wait mid-loop.
- The follow loop re-reads the reply's length each tick, and an idle player is
  only the end of the reply once every sentence has been resolved — otherwise
  it is a gap while the tail renders.
- The archive resolves the tail first, so a reply played in pieces is still
  recorded whole.

Tests: `packages/core/tests/test_stream_clips.py`. The idle guard is the
load-bearing one — with it removed, that test fails.

Not measured on the phone yet.
