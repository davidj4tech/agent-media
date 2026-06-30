# Grade B — phone-rendered speech with red5's popup following

Status: scoping. Goal: **0-latency speech audio on the phone AND the full popup**
(now_playing, sentence highlight, skip/replay), while Claude stays on red5.

## The core idea

The popup lives wherever the Claude session/tmux runs — **red5**. The audio
doesn't have to. So:

- **red5** keeps the whole speech *orchestration*: sentence split, `now_playing`
  per sentence, the highlight scheduler, nav (skip/replay), duck, the playback
  lock. The popup reads exactly the state it does today.
- **The phone** does the *render + playout*, locally (edge-tts → its own
  sink-speech mpv). Only the **text** crosses Germany→AU; audio never leaves the
  phone, so playback is ~0-latency.

The two are stitched by driving the phone's sink-speech mpv from red5 over an
IPC bridge, and pre-rendering clips on the phone so each play is immediate (which
is what keeps the highlight in sync).

Contrast:
- Today's phone-local bridge (`MEDIA_REMOTE_SAY_CMD`): phone renders+plays the
  *whole reply* in one shot → audio works, but red5's sentence loop never runs,
  so **no popup**.
- Grade A: red5 renders clips, ships each to the phone to play → popup intact but
  audio (clip) crosses Germany→AU each sentence.
- **Grade B (this):** phone renders+plays per sentence, red5 orchestrates → popup
  intact *and* low latency.

## How the highlight stays in sync

The local path aligns the highlight to playback by offsetting it
`MEDIA_SNAPCAST_LATENCY_MS` (~the Snapcast buffer). Grade B has no Snapcast
buffer: clips are **pre-rendered on the phone** (in parallel, up front, exactly
like red5's loop pre-renders today), so the per-sentence `play` is just an mpv
`loadfile` of an already-local file — it starts in tens of ms. red5 fires the
highlight when it issues the play, offset by a small `MEDIA_PHONE_PLAYOUT_MS`
(tcp round-trip + mpv start) instead of the ~1 s Snapcast buffer. Net: tighter
sync than the rooms path, not looser.

## Pieces to build

1. **Phone sink-speech TCP bridge** — expose the phone's `sink-speech.sock` on a
   Tailscale TCP port (reuse the `mpv-music-bridge` socat runit service pattern
   already deployed). Lets red5's `_mpv_ipc` drive it via `tcp://…`.
2. **Phone-side render-to-clip** — render each sentence to a clip on the phone
   (edge-tts, AU IP). Reuse the phone's own `render_text`; expose a small
   "render these sentences → these paths" entry the loop can call (batched,
   parallel). Clips land in a phone temp dir.
3. **`SinkSpeech` phone target** — `MEDIA_SPEECH_SOCKET_PHONE=tcp://<phone>:<port>`
   + a `phone` case in `_device_for` (→ the phone mpv's own default device, i.e.
   `None`). Then `SinkSpeech.play(phone_clip, phone_target)` loads the
   phone-local clip on the phone's mpv. `idle/pause/position/paused/muted` already
   work over the same socket (they're socket-generic).
4. **submit-loop branch** — a `phone-render` mode in `submit_event`: render the
   sentence clips on the phone (step 2) instead of locally, then run the existing
   play loop against the phone target (step 3). `now_playing`, highlight, nav,
   playback lock — all unchanged; only *where* render+play happen moves.
5. **Duck** — red5's coordinator ducks the **phone's** music via the Stage-1
   phone backend (`MEDIA_MUSIC_LOCAL_ENDPOINT` = the phone's mpv-music TCP
   bridge, already built). So music dips under speech on the device.

## Work / risk

- Reuses a lot: the bridge pattern, `_mpv_ipc` tcp support, `SinkSpeech`'s
  socket abstraction, the Stage-1 phone duck. The genuinely new bits are the
  phone render-to-clip entry and the submit-loop `phone-render` branch.
- Rough estimate: a couple of focused hours + live testing on the phone.
- Retire the `MEDIA_REMOTE_SAY_CMD` bridge once this lands (it becomes the
  fallback for when the phone's unreachable).

## Open questions

- **First-sentence latency:** the first clip still waits on its edge-tts render
  (unavoidable, same as today); later sentences are pre-rendered in parallel so
  they're instant. Acceptable.
- **Phone asleep / unreachable mid-reply:** fall back to red5 local render (rooms)
  or the whole-reply bridge? Pick a graceful degrade.
- **Shared sink-speech broker:** the phone's mpv is also used by the phone's own
  `media say`; fine while sessions run only on red5, but worth a guard.
- **Clip cleanup** on the phone (temp dir GC).
- **Nav across the bridge:** skip/replay drive the phone mpv's position over tcp —
  verify latency is acceptable for snappy popup controls.
