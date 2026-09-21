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

## On the phone (21 Sep, app target) — rendering was never the long pole

Streaming works end to end: a 7-sentence reply started holding 2 clips, the
list grew to 4, 6, 7 during playback, and it played to the end.

But submit-to-first-audio was still **8.3s**, and a stage-timed run (scratchpad
`time_stages.py`) shows why. Past the queue for the global speech token:

| stage | took |
| --- | --- |
| render + probe, 2 sentences | 1.0s |
| `_wait_and_claim_broker` | 5.8s |
| `coord.before_speech` | 12.8s |
| — its book / music probes (parallel) | 0.9s / 4.1s |
| `sink.play_playlist` | 0.7s |

`before_speech` pauses the book and pauses or ducks the music over the phone
bridge, 1–4s a round trip, partly in series. Neither it nor the broker claim
overlaps rendering: Phase 1 finishes before the token is even taken. So
streaming trims render time off a path that is mostly bridge round trips.

Remote-pause hosts (MPRIS/Android) are empty on red5, so the ~4.8s SSH
cold-connect that `pre_pause_remote` exists to hide is not in play here.

The next win is the pre-speech path, not the renderer: the broker claim and
the pause/duck round trips, or overlapping them with the render.

## Where a round trip goes, and the first fix (21 Sep)

Each IPC call to the phone opens a fresh TCP connection. With the phone on a
~430ms tailnet path (tailscale ping 429ms, via a public IP — off home Wi-Fi):

| call | took |
| --- | --- |
| TCP connect alone | 0.44s |
| `get_property`, one | 0.87s |
| `get_properties`, five | 1.30s |

So a call is a connect plus a request, and nothing on the phone is slow. An
uncontended broker claim makes four of them (read, read again inside
`claim_broker`, write, read back after a desync sleep).

`d2ff126` runs the claim (and the prefetch) beside `before_speech` instead of
ahead of it. Re-timed on a quieter link with nothing to pause: claim 2.7s and
before_speech 2.5s, now concurrent — the pre-speech path costs the longer of
the two instead of both.

Not done, in order of value:

1. ~~Reuse one connection per endpoint within a reply~~ — DONE `46e3d52`.
   Live, five reads to the app: 4.37s fresh, 2.62s shared (first 0.87s,
   then 0.43s each). Scoped to one reply; replies matched by unique
   request_id; a dead pooled socket is retried fresh, not breakered.
2. Drop the redundant owner read in `claim_broker` (4 calls -> 3).

Side finding: at this link speed the music endpoint's calls exceed the 1.2s
slow line and trip its 20s breaker (it was open when first probed). While it
is open, non-critical music calls are skipped — including the probe that
decides whether to duck music under speech.

FIXED `86763f0`: the budget for a policy call is now four round trips on its
link (fastest recent connect, capped at 3s), not a flat 1.2s. Live: reads of
0.89-1.32s against a 2.5s budget left the music breaker shut.

## After the round of cuts (21 Sep, evening)

Committed, each with a test that fails without it:

| commit | cut |
| --- | --- |
| `d2ff126` | broker claim runs beside before_speech |
| `86763f0` | slow-call breaker budget scales with the link (music ducks again) |
| `46e3d52` | connections reused within a reply (0.87s -> 0.43s per call after the first) |
| `872b6ee` | no book probe against the speech player (was 1.9s, always "no") |
| `056f1dc` | broker claim reads the owner once |
| `1cc0b8e` | streaming: the lead renders behind the pre-speech round trips |
| `a0a67d5` | before_speech resolves the music backend once, not three times |

Measured from taking the token to the playlist being sent, music loaded on
the phone: 7.2s -> 5.0s (before_speech 6.2s -> 4.1s; position + pause after
the probe ~3.5s -> 0.9s). Without music, 4.3s measured before the last three
cuts; not re-measured after them.

Left, in rough order of value:

1. **The phone's own part — MEASURED, 2.45s** (scratchpad
   `measure_audible.py`: each snapshot's read time minus its time-pos gives
   when the clip began; three reads agreed to 10ms). Submit -> audible ~6.8s
   end to end, no hold, no queue: 4.3s on red5, 2.45s on the phone, of which
   ~0.4s is the command in transit and the rest is most likely the app
   fetching clip 0 over HTTP from red5 plus decoder start. Clip 1's start
   estimates wandered by ~0.5s — probably a buffering stall while it was
   still downloading (inferred, not confirmed).

   Lever: Sasonica starts fetching a clip the moment it is appended, even to
   an idle player (BuiltinSpeech.warm). So the playlist could be LOADED right
   after the broker claim — the claim is what makes the player ours — and
   only STARTED (playlist-pos 0) once before_speech is done. With music on,
   before_speech outlasts the claim by ~2s, which would hide the whole fetch.
   Without music, it gains little. Termux mpv does not fetch on append.

   BUILT `ec24a85`. Measured with music loaded but PAUSED: start -> audible
   2.02s (was 2.45s); the load's head start over the start was only 0.74s,
   because before_speech barely outlasted the claim. With music playing it
   should run ~2s longer than the claim — enough for the whole fetch — but
   that case is not measured yet.
2. The music probe (3.2s) resolves app and phone liveness one after the
   other; they are independent and could be asked together (~0.9s).
3. Nagle on the phone: batched reads cost two round trips because the
   server holds every reply after the first until the first is ACKed.
   `setTcpNoDelay(true)` in Sasonica's MpvServer (an APK), `nodelay` on socat.
