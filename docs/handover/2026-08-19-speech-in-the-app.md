# Handover — speech plays in the app now

Written 2026-08-19, the evening the spike in
`2026-08-18-player-on-the-phone.md` was answered and the player built. That
file's day-one question is closed; this one supersedes it. The proposal
(`docs/proposals/2026-08-18-player-on-the-phone.md`) still reads true apart
from the transport, which David redirected — see "The socket stayed".

## The spike's answer

**`android.media.MediaPlayer` holds the requested speed exactly.** Measured on
p8a as media-position advance over wall clock, with pitch pinned and
`AUDIO_FALLBACK_MODE_FAIL`: 1.602 at 1.6×, 1.600 when changed mid-play, 1.998
at 2.0×, 1.599 from a local file. David judged the ear test good.
`[[pinned-scaletempo2-eats-speed]]` does **not** reproduce.

So: **no Media3, no AndroidX, no Gradle.** `build.sh` survives intact. The
spike is `spikes/mediaplayer-speed/`, with both runs' tables kept beside it.

Two things it found on the way, both of which shaped the player:

- **Fetch, then play.** `prepare()` over HTTP measured 8.5–9.8s against 44–78ms
  from a local file, with rebuffers mid-clip. Run 1's 1.0× "failure" (0.904)
  was that rebuffer, proven by a local-file control in run 2 rather than
  argued.
- **The player needs a foreground service.** Backgrounded, ActivityManager
  froze the process a minute in — threads stopped mid-trial, nothing logged.
  A player that only works while its screen is up fails on most replies.

## The socket stayed, and that is the design

The proposal assumed a new transport (SSE, a per-device command stream).
David's steer was better: **keep the socket, move the player.** The app answers
mpv's JSON IPC itself, so `sinks/speech` keeps writing `loadfile` and reading
`playlist-pos` back and never learns who is on the other end.

- `MpvServer` — the protocol, `android.*`-free, tested against the byte
  sequences `SinkSpeech` actually sends (`play`'s loadfile plus the pause/mute
  reset; `play_playlist`'s whole batch). Unknown properties answer
  `property not found`, which is what an mpv too old for `user-data` already
  answered, so the sink's best-effort writes meet a failure they tolerate.
- `BuiltinSpeech` — the player behind it. Downloads each clip, prepares the
  next while the current plays, and hands it over with `setNextMediaPlayer`
  (gap went from ~0.7s to 0.16–0.34s, some of which is mp3 padding).
- It binds **the tailnet address, never 0.0.0.0**: mpv's IPC has no
  authentication and never had any.

**Coexistence is per-target, not a switch.** 6602 keeps the Termux mpv and its
socat bridge; the app listens on 6612; a target moves between them with
`MEDIA_SPEECH_SOCKET_<TARGET>`. `media-lane` owns
`MEDIA_SPEECH_DEFAULT_TARGET` in every state, so the trial switch is
`MEDIA_SPEECH_PHONE_TARGET=app` in `~/.config/agent-media.env` (dotfiles
commit `dbb2d30`). Back is that line set to `phone` plus one `media-lane` run.

`Server.BUILTIN` stays refused on purpose: it would move music and the book
too, and those have no player here.

## What using it in anger found, which no test would have

1. **An in-app player must not request audio focus.** `FocusControl` already
   owns it; our request read as a loss, abandoning re-granted, and the grant
   came back as a change to pause on — five times a second. David: "very
   jittery."
2. **`setPlaybackParams` with a non-zero speed STARTS a player that is not
   started.** Preparing the next clip with its speed applied began playing it
   under the current one. Speed goes on at adoption instead.
3. **The app's own policies drove the wrong player.** Barge-in, the dictation
   hold and the focus rules all wrote to mpv's socket. `performSpeech` now acts
   on whichever player has a clip open, and `SideChannel` renders whichever is
   speaking — see `108c38d`.
4. **"Something is recording" has not meant "David is talking" for a long
   time.** `com.google.android.as` cycles this phone's microphone constantly —
   ~650ms holds normally, **~2s while our audio plays** (Now Playing trying to
   identify the "music"). Every burst paused speech. Invisible on mpv, audible
   in-app. Fixed in three layers: `MicSteady` (sustain, learned baseline, and
   "assume sampling until proven otherwise" for the first minute after a
   restart), ignoring **silenced** recordings, and an app-ops denial. See
   `[[mic-baseline-is-permanent]]`.

## The tooling that changed underneath all of it

**p8a has adb shell uid, driven from red5.** The phone's own adb pairs to its
own adbd over loopback (`[[adb-shell-via-self-pairing]]`). `pm revoke` is not
enough for a role-granted permission; app-ops is. Installs, `logcat`,
`dumpsys` and `pm grant` all work from red5 now, and `android/companion/deploy.sh`
is the loop: tests, build, install, then knock **`WakeActivity`** — never
`MainActivity`, which is the diagnostic screen and takes the foreground.

## Pick up here

- **The trial is running.** Speech comes from the app until
  `MEDIA_SPEECH_PHONE_TARGET` says otherwise. Watch for: a batch timeout seen
  once (`play_playlist` timed out while the reply itself played fine — Doze is
  the suspect), and whether the ~2s tail after David stops talking feels right.
- **Barge-in's cost is unmeasured.** With the recogniser silenced the duration
  rule works, but if anything re-grants it the mic, `MicSteady` falls back to
  requiring a second concurrent recording — and Gboard alone is only one.
- **`seek` is not implemented.** The speech sink never sends it; the popup
  might.
- **Music and book are still mpv**, and should stay there until speech has
  been in the app long enough to trust. The proposal's sequencing holds.
- **The `+dirty` lesson.** For part of tonight the phone ran an APK that
  existed in no git history — two sessions' uncommitted edits over one commit.
  `/state` reports the build stamp; if it ends in `+dirty`, nobody can say
  what is on the phone.
