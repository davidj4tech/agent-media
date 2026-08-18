# Handover — start the player-on-the-phone work

Written 2026-08-18 by the session that built the app's client/server
configuration. Read `docs/proposals/2026-08-18-player-on-the-phone.md` first;
this file is only what to do on day one and what is already true.

## What is already true

- **The app has a client/server configuration** (`7ba28a8`, `94296cb`,
  `4637bc6`, all on main). `Server.java` holds the address, the token and the
  playback location; `Server.BUILTIN` — playing in the app — is named and
  refused by `Server.problem()`. That is the slot this work fills.
- **The mpv bridges follow the sound, not the server** (`Server.mpvHost()`), so
  a remote server with phone-local audio already works.
- **`media-share` runs on red5**: `agent-media-share.service`, bound to
  `100.103.43.93:8771`, token required (`MEDIA_SHARE_TOKEN` in
  `~/.config/agent-media.env`). Verified reachable from p8a.
- **red5 already serves rendered clips over HTTP** — `agent-media-clips.service`
  on `:8780`, tailnet-only, from `~/.cache/agent-media`. The speech half of this
  work has its audio transport already.
- The app is **not** yet sideloaded with any of this; the APK is sitting in
  p8a's `~/storage/downloads/`.

## Day one: one spike, one question

**Does `android.media.MediaPlayer` play speech at 1.6× with pitch correction
well enough to ship?** `PlaybackParams` with
`setAudioFallbackMode(AUDIO_FALLBACK_MODE_FAIL)` and a speed of 1.6, against a
real rendered clip from `~/.cache/agent-media` on red5.

It decides the toolchain, which decides everything after it:

- **Good enough** → the no-Gradle build survives (`build.sh` is aapt2 + javac +
  d8, no AndroidX, no Maven, because red5 is near 90% disk). Speech-in-app is a
  couple of days on platform APIs.
- **Not good enough** → Media3/ExoPlayer becomes mandatory, and the first real
  decision is how to get AndroidX into this build at all — Gradle, or
  hand-unpacked AARs. Settle that before writing player code.

Watch for the trap the *other* player already hit here: a pinned `scaletempo2`
never gets the new speed, and `pitch-correction=no` resamples. See
`[[pinned-scaletempo2-eats-speed]]` in memory — the symptom was 1.6 requested,
1.18 actual.

## Constraints that are not up for revisiting

- **No Gradle, no AndroidX, no Maven** unless the spike forces it, and then say
  so out loud rather than drifting into it.
- **Speech only.** Music and book stay on mpv. The proposal's whole argument for
  coexistence is that the split stays narrow.
- **Only music opens an `AudioTrack`** and holds the addressed-player slot. An
  in-app speech player must not take it.
- Every device test is a sideload and a squint: `./test/run.sh` on the build
  host first, always. `scp` the APK to `p8a:~/storage/downloads/` and David
  opens it from Files.

## After the spike

Sequencing is in the proposal: speech, then music, then book. The next design
question after the player itself is the **control reversal** — today the server
writes into mpv's socket (`SinkMusicLocal`, `sinks/speech`); with a player in
the app the server has to tell the app what to play. Precedent for both halves
already exists (clips over HTTP on 8780, SSE on the visual canvas).
