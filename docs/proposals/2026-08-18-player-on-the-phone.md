# A player on the phone — what it would take to stop needing Termux

Status: proposal, nothing built. 2026-08-18. Written from the morning the
companion app grew a client/server configuration (`7ba28a8`), which is what put
the question on the table: the app can now be told where its server is, and the
obvious next question is whether it needs one on *this* phone at all.

## Recommendation in one line

Give the app **its own player, on the speech channel only**, and leave music and
book on mpv. Not "replace mpv" — the two coexist per channel, and the config
already has the slot for it (`Server.BUILTIN`, named and refused today).

The three-word version of why: **speech is where focus actually matters**, clips
are seconds long, red5 already serves them over HTTP, and a failure costs one
sentence rather than an evening's music.

## The question underneath

Everything phone-side in this project is downstream of one fact: **mpv ignores
Android audio focus.** `call_guard`, the external-hold flag, the silent
`AudioTrack`, `FocusPolicy`, `SpeechPolicy`, the duck/restore bookkeeping, the
`isMusicActive()` quiet-poll — every one of them exists to hold focus on behalf
of a player that will not hold it itself.

A player inside the app does not need any of that. Android ducks our stream
because it is our stream. That is the prize, and it is worth more than the
Termux dependency it also removes.

## What "opt-in mpv install" cannot be

Raised 2026-08-18: let the app offer to install mpv and yt-dlp itself, behind an
opt-in and a licence disclosure. Three separate walls, and only one of them is
about licences.

**A click-through does not do the legal work.** GPL obligations attach to
distribution regardless of what the user agrees to; you cannot EULA your way
into a licence you do not hold. What changes the position is either not
distributing the GPL code at all, or licensing our own app compatibly.

**Android will not execute a downloaded binary.** Since API 29 an app cannot
`exec` a file from its writable storage — only what was extracted from the APK
at install time. Termux survives by pinning an ancient `targetSdk`; the
companion is on 35. "Fetch an mpv build at runtime" is dead on arrival,
licences aside.

**yt-dlp is Python**, so it needs an interpreter shipped inside the APK rather
than downloaded. Its own licence is the easy one — Unlicense, no obligations.

So the shapes that survive are:

| Route | What we distribute | Licence position |
|---|---|---|
| **Guided install of a separate app** — detect no player, offer a tap through to Termux or mpv-android on F-Droid, then set up the bridges | nothing | none: we link, we do not ship |
| **A GPL build of our own app**, libmpv inside, on F-Droid | the combined work | the whole app becomes GPLv3. Apache-2.0 moves one way into GPLv3, and mpv is GPLv2+ so it can be taken as v3 |
| **A player of our own** (below) | our code only | no GPL anywhere |

The first is the honest version of the opt-in idea and is worth building
whatever else happens: it turns the README's "requires three socat listeners"
into onboarding. The second is a decision about what this project *is*, not a
feature flag. The third is the rest of this document.

## ExoPlayer, or the platform's own MediaPlayer

**The toolchain is the real cost, not the playback code.** Media3/ExoPlayer is
AndroidX: AARs from Maven with transitive dependencies. This app has no Gradle,
no AndroidX and no Maven on purpose — `build.sh` is aapt2, javac and d8, because
red5 sits near 90% disk and an AGP install is several GB against a few hundred
MB for what we have. Adopting Media3 means adopting that, or hand-unpacking AARs
and merging their manifests, which is a job that does not end.

`android.media.MediaPlayer` costs none of it. Zero dependencies, progressive
HTTP and local files, and `PlaybackParams` gives speed with pitch correction —
which is the one mpv feature the speech channel actually leans on. Our sources
are rendered TTS files and fetched audio files: no HLS, no DASH, nothing that
needs adaptive streaming.

ExoPlayer earns its keep on gapless playback, error recovery and format edge
cases. Those are v2 arguments, not the price of admission. **Start on
MediaPlayer; the interface the app talks to should be ours, so the engine
underneath can change without the rest moving.**

## The reversal this actually requires

Today the server drives the phone by **writing into mpv's socket** —
`SinkMusicLocal` speaks the JSON IPC contract, `sinks/speech` stages clips and
unpauses the broker. A player inside the app inverts that: the server has to
*tell the app* what to play, and the app fetches the audio.

Both halves already exist in this fleet:

- **Audio over HTTP** — `agent-media-clips.service` serves
  `~/.cache/agent-media` on red5:8780, tailnet-only, because the phone already
  fetches rendered clips that way.
- **Commands over a stream** — the visual canvas holds an SSE connection from
  the phone to red5. The same shape carries "play this, pause, resume".

So the new work is a per-device command stream and a play/queue verb, not a new
transport. `media-share` (8771) is already the control API in the other
direction; this is its mirror.

## Coexistence with mpv, which is the point

The channels are independent by construction: each has its own session, its own
card, its own IPC connection and its own policy class. Nothing requires them to
play the same way, and the mixed arrangement is the recommended one.

Three things change when playback becomes per-channel:

1. **`Server.playback` becomes a map**, channel → location, defaulting to the
   single value it holds today. Additive; nothing stored now becomes invalid.
2. **`ownsThePhonesAudio()` stops being one boolean.** For an mpv-driven channel
   we hold focus on its behalf and duck it over IPC. For an in-app channel
   Android ducks us directly and we mostly want to get out of the way — the
   `setWillPauseWhenDucked(true)` reasoning inverts, because there the callback
   was bought to compensate for a stream that was not really ours.
3. **The addressed-player slot stays with music**, as it is now. It is a
   per-phone singleton, the earbud addresses it, and speech never wanted it.
   The silent `AudioTrack` survives exactly as long as music is on mpv.

The real cost of coexistence is not architectural: it is **two playout paths to
keep alive, and a diagnosis question that doubles** — when something is silent,
which player was holding it. That argues for keeping the split narrow (one
channel in the app) rather than offering a matrix of every channel either way.

## Sequencing

1. **Speech in-app.** Fetch the clip over HTTP, play it, publish the same card.
   Focus becomes honest on the channel that needed it most. `SpeechPolicy`'s
   pause/resume/deadline logic mostly evaporates; what remains is a queue.
2. **Music.** Drags in the queue, the offline cache, chapters and the
   addressed-player slot. Only worth it once speech has been living in the app
   long enough to trust.
3. **Book.** Last: hour-long files, position persistence across restarts, and
   chapter navigation the app would have to read from the server rather than
   from mpv.

The guided-install route above is orthogonal and can land at any point.

## What we would lose

- **dynaudnorm.** No equivalent in either player. `DynamicsProcessing`
  (API 28+) could approximate it; unverified.
- **Format coverage.** Irrelevant for TTS output, a real question for whatever
  yt-dlp hands back. The server can normalise on the way out.
- **The offline cache**, which stays server-side and is arguably where it
  belongs — the fetch has to happen on the residential IP anyway.
- **The most battle-tested part of the system.** The phone playout path has had
  a year of failures found and fixed. Moving it means finding some of them
  again, which is the strongest argument for taking one channel at a time.

## Open questions

- Whether `MediaPlayer`'s speed control is good enough for speech at 1.6×, or
  whether that alone forces Media3. Testable in an afternoon.
- Whether a long-lived SSE connection survives Doze on this phone, or whether
  the command stream needs to be poll-based when the screen is off.
- Whether the app should cache clips at all, or stream each one — a reply is
  many short clips, and the connection is the slow part.
- What drives the speech queue when the app is killed mid-reply. Today the
  broker holds the clip and the coordinator clears `pause` on the next
  response; an in-app queue has no equivalent backstop yet.
