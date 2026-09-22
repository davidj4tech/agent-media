# Proposal: voice chat with barge-in (22 Sep 2026)

Status: **plan, nothing built.** The goal David asked for is "full duplex
chat". What this delivers is the practical version of it: the phone listens
the whole time a voice chat is open, the reply starts speaking within a
couple of seconds of him stopping, and talking over it cuts it off at once
and becomes the next message. A single model that listens and speaks at the
same moment (Moshi-style) needs a GPU and is out of scope. Nothing here
needs more than red5's CPU.

## 1. What exists, and what is missing

What exists already:

| Piece | Where | State |
|---|---|---|
| Speaking | `intake/submit.py` render pool; piper ~0.21s a sentence, edge ~0.6s | fast, but only runs when a turn **ends** (Stop hook) |
| Sentence-at-a-time speech | `intake/hook_pi_stream.py` (`IncrementalSentencer`, `submit_stream`) | pi only |
| A session that can stream | headless driver + `sessiond.py` (`claude -p` stream-json) | behind `MEDIA_HEADLESS`; `stream_event`s thrown away (`sessiond.py:532`) |
| Interrupting a turn | `POST /session/stop` (contract §12), headless `interrupt` | works; press-driven |
| Noticing the mic | old app `MicWatch` / `BargeIn` | sees *another* app recording; pauses speech, doesn't cut it; no RECORD_AUDIO |
| Hearing words | Next `SpeechInputPlugin` (`ACTION_RECOGNIZE_SPEECH`) | one-shot system dialog, no partials, not continuous |
| Getting text back | `GET /threads/{s}/events` SSE | whole messages only, ~0.33s after the transcript line |

What's missing:

1. **Continuous listening in the app** (`SpeechRecognizer` in-process, not the dialog).
2. **Reply text as it is written** (`--include-partial-messages`, forwarded).
3. **Speech that starts on the first sentence**, not when the turn ends.
4. **One path from "he started talking" to "stop the audio and interrupt the turn"**, decided on the phone, with no round trip.
5. **A player in Sasonica Next.** It has none yet (already queued: "reuse the companion code").
6. **Echo control**, so the phone doesn't hear its own voice and interrupt itself.

## 2. Shape

```
 phone (Sasonica Next)                          red5
 ┌──────────────────────────┐   one WebSocket   ┌────────────────────────────┐
 │ SpeechRecognizer (cont.) │ ── final text ──▶ │ /voice/{session}            │
 │   onBeginningOfSpeech ───┼─ local cut        │  → headless send            │
 │ player (VOICE_COMM usage)│ ◀─ audio chunks ─ │  ← partial text deltas      │
 │   AEC on                 │ ◀─ captions ───── │  → sentencer → piper/edge   │
 │ barge-in: stop + "cut@N" │ ── cut@N ───────▶ │  → interrupt + note heard   │
 └──────────────────────────┘                   └────────────────────────────┘
```

- **One socket per voice chat.** Every IPC today is a fresh TCP connect plus a
  request (~0.9s at 430ms RTT, `speech-latency-notes.md`). An open WebSocket
  leaves ~0.2s each way. Audio travels in it, so there is no fetch of a clip
  URL.
- **Voice mode bypasses the broker.** The book and music ducking in
  `before_speech` costs ~4.3s on red5 today. In voice mode the app owns audio
  focus itself, so the server only renders and sends.
- **Barge-in is decided on the phone.** `onBeginningOfSpeech`, or the first
  partial result, stops playback locally at once (0 RTT). The phone then sends
  `cut@N` (the index of the sentence it was on). The server interrupts the
  turn and records how far the reply actually got.
- **What was heard is what counts.** The next message goes to the model as
  "(you were cut off after: '…')" plus his words, so it doesn't assume he
  heard the unsaid rest.

## 3. Latency budget (honest)

From the end of his sentence to the first audio:

| Step | Estimate |
|---|---|
| recogniser decides he has stopped (endpointing) | 0.5–0.8s |
| text up the open socket | ~0.2s |
| model's first sentence: haiku / opus | ~1.0–1.5s / ~4–5s |
| piper renders it | ~0.2s |
| audio down + playback starts | ~0.3s |
| **total** | **~2.2–3s on haiku; ~5–6s on opus** |

"About one second" isn't reachable on this path. **About two** is, on a fast
model. Two later levers:

- **A spoken acknowledgement** on opus ("Mm, let me look"), rendered locally
  from a small cached set, masks the wait.
- **Speculative send** on a stable partial transcript can win back ~0.5s.
  It risks answering half a sentence, so it comes last and only if measured
  to help.

## 4. Steps

0. **Spike (server only, ~half a day).** Headless haiku with
   `--include-partial-messages`, deltas to the pi sentencer, then piper, then
   a WebSocket, with a tiny test client on red5 that timestamps everything.
   Measure text in to first audio byte. Go/no-go on the ~2s target.
1. **`/voice/{session}` on the server.** Paired-device token auth (contract
   §9). Messages up: `text`, `cut`, `bye`. Messages down: `audio`, `caption`,
   `state`. Headless sessions only at first; a pane session can't stream
   partial text.
2. **Player in Next.** The queued "reuse the companion code" item, plus
   playing audio chunks straight from the socket. It uses
   `USAGE_VOICE_COMMUNICATION` so Android's echo canceller applies on
   speaker.
3. **Continuous listening in Next.** `SpeechRecognizer` with partial
   results, `EXTRA_PREFER_OFFLINE` (on-device on a Pixel), restarted after
   each utterance, with a mic-on indicator and a tap to mute. Barge-in is
   local, as in §2.
4. **Interrupt semantics.** `cut@N` goes to the headless `interrupt`, the
   heard text is trimmed, and the next message carries the cut-off note. It
   reuses `/session/stop`'s rules (speech already said stays said).
5. **Polish.** A model choice for voice chats, acknowledgements, the book and
   music ducking in-app, a hold-to-talk fallback for noisy places, and
   captions in the thread.

## 5. Risks

- **Echo on speaker.** If AEC doesn't hold, the recogniser hears the reply
  and cuts it off. The fallbacks are earbuds, or "barge-in needs 2+ words of
  partial text" rather than any sound. Measure in step 3.
- **Pixel's recogniser across restarts.** Continuous listening means a new
  session per utterance, with a gap of ~100–300ms and a beep on some builds.
  If it's bad, the fallback is streaming mic audio up and running Whisper
  (small, CPU) on red5, which costs more upload and CPU.
- **Google's own mic use** (`mic-baseline-is-permanent.md`). It doesn't
  matter here: the app records itself and no longer infers from others.
- **Headless only.** Voice chats won't attach to a tmux pane session until
  pane sessions can stream partial text.

## 6. Questions for David

- Which app: Next (recommended; the old app is retiring) or the old app?
- Model for voice chats: haiku by default for speed, or the session's model?
- Speaker or earbuds, mostly? It decides how much echo work step 3 carries.
