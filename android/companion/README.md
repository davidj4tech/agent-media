# agent-media companion (Android, p8a)

Publishes **one MediaSession** for the music channel so earbud, lock-screen and
car-stereo transport controls reach agent-media, and drives the Termux mpv that
does the actual playing over **loopback TCP**.

It is not a player. The only audio it opens is a stream of zeros, because
Android will not give the Bluetooth addressed-player slot to a session with no
open stream — that cost three spike runs to learn, see
`docs/proposals/2026-08-13-mediasession-spike.md`. mpv keeps the residential
yt-dlp fetch, the offline cache, `--ao=pulse`, dynaudnorm and the JSON IPC
contract `SinkMusicLocal` speaks.

Design and rejected alternatives:
`docs/proposals/2026-08-13-android-companion-app.md`.

## Status — 2026-08-14: working on p8a

Installed and driving mpv. **Pause, next and previous all reach Termux mpv from
the earbuds**: the key travels earbud → AVRCP → our MediaSession → mpv IPC →
mpv. Verified against a four-entry playlist — `playlist-pos` moved 0 → 1 → back
on the presses, and mpv's log stops dead at the pause position. That is the path
this whole design exists to open.

Building a playlist to test with: the phone backend expands nothing —
`play-local` passes `--no-playlist` to yt-dlp deliberately — so queue track by
track with `media music play <uri> --where phone --add`. Expect the odd 403 and
the occasional fetch past the 120 s default (`MEDIA_MUSIC_LOCAL_FETCH_TIMEOUT`);
both cleared on a retry. Unloaded queue entries show as bare filenames because
mpv fills a playlist entry's title only once it loads the file — the metadata is
correct by the time it plays.

Not yet verified: car-display metadata from the real app rather than the spike,
and the lock screen.

Uninstall the spike before testing this. Both publish a session, and the spike
holds its silent track permanently, so it competes for the same
addressed-player slot.

## Layout

| File | What it is |
|---|---|
| `Json.java` | Minimal JSON codec. No `org.json`, no `android.*` — that is what makes the IPC testable off-device. |
| `MpvIpc.java` | The mpv JSON IPC client: line protocol, `request_id` correlation, `observe_property`, reconnect with backoff. Also `android.*`-free. |
| `MpvState.java` | The mirrored mpv properties. `loaded()`/`playing()` are deliberately the same predicates `agent_media_core.sinks.music_local` uses. |
| `FrontChannel.java` | Which channel the session's metadata describes — speech while a clip runs, music otherwise. `android.*`-free, so `test/run.sh` covers it. |
| `FocusPolicy.java` | The audio-focus decision table — what to do with mpv when focus moves, and what is owed back afterwards. `android.*`-free, so `test/run.sh` covers it. |
| `FocusControl.java` | The `android.*` half of focus: request, abandon, forward the callbacks. Also the tripwire on `FocusPolicy`'s duplicated constants. |
| `CompanionService.java` | Session, notification, the silent `AudioTrack`, and the wiring in both directions. |
| `StatusServer.java` | The readout the outside can reach: `/state` and `/log` over loopback HTTP. `android.*`-free, so `test/run.sh` covers it. |
| `MainActivity.java` | The on-screen readout — state and an event log, plus the probe/acting button. |
| `MediaButtonReceiver.java` | Logs the broadcast path. Handles nothing; exists for diagnosis. |

## Build and test

```sh
./test/run.sh     # host-side: Json, MpvIpc, MpvState against a fake mpv. Fast.
./build.sh        # -> build/agent-media-companion.apk  (~28 KB)
```

`build.sh` needs `$ANDROID_HOME` (default `~/android-sdk`) with build-tools
35.0.0 and the android-35 platform, plus a JDK. No Gradle, no AndroidX, nothing
from Maven — red5 sits near 90% disk and this keeps the toolchain at a few
hundred MB.

**Keep `debug.keystore`.** It is gitignored, and Android refuses to upgrade an
installed app signed by a different key — losing it means uninstalling on the
phone and re-granting permissions, which is a nuisance when every install is a
sideload.

## Phone side

Requires two socat listeners on loopback, into the two mpv IPC sockets: mpv's
sockets live inside `com.termux`'s private UID sandbox and no other app can open
them.

| Service (dotfiles `termux/services/`) | Listener | Socket | What it carries |
|---|---|---|---|
| `mpv-music-bridge-local` | `127.0.0.1:6601` | `mpv-music.sock` | transport, focus actions, metadata |
| `mpv-speech-bridge-local` | `127.0.0.1:6602` | `sink-speech.sock` | read-only so far: is a clip playing |

Both are **separate** services from `mpv-music-bridge` / `mpv-speech-bridge`,
which bind the Tailscale address only and must not be touched. Same port numbers
are fine — different bind address. Both are declared in `ansible/host_vars/p8a.yml`.

Install: `scp` the APK to `~/storage/downloads/` on the phone, and David opens
it from Files. That is the whole recipe — `termux-open --chooser` does not
reliably raise the installer dialog on p8a, so do not offer it. adb cannot
reach p8a from red5 either (adbd binds wlan0 only).

## Reading what it is doing

```sh
ssh p8a curl -s 127.0.0.1:8770/state   # JSON: mpv state, focus mode, what is owed
ssh p8a curl -s 127.0.0.1:8770/log     # the event log, newest first
ssh p8a curl -s 127.0.0.1:8770/        # both
```

Bound to loopback only, like `mpv-music-bridge-local` and for the same reason —
never widen it. Everything that needs it already has a shell on the phone.

This exists because the alternatives do not work here: `logcat` from Termux
shows only Termux's own uid, `dumpsys media_session` is refused to a non-shell
uid, and adb cannot reach p8a from red5. Before it, diagnosing the app meant
asking David to read his phone screen aloud.

`/state` answers the questions the on-screen readout was being asked for:
`focus_mode` (probe or acting), `focus_held`, `owes_resume` / `owes_unduck`,
`restore_volume`, and `focus_events` — every focus callback the app has seen,
timestamped.

## Audio focus

The app holds audio focus **on mpv's behalf** — mpv ignores it, which is the
root of most of the phone-side complexity agent-media carries. Focus follows
`loaded()`, the same predicate the silent track uses, so it is held while a file
is open *including while paused*: abandoning it on our own pause would forfeit
the `GAIN` that says to resume.

David's rule is **duck the music, pause the speech**. This is the music half.
The speech bridge now exists, but the app only *listens* on it (see below); a
focus loss still does nothing to speech.

| Focus change | Music | Owed afterwards |
|---|---|---|
| `LOSS` | restore volume, then pause | nothing — a permanent loss is not followed by a resume, and music restarting minutes later is worse than a button press |
| `LOSS_TRANSIENT` | restore volume, then pause | resume on `GAIN` |
| `LOSS_TRANSIENT_CAN_DUCK` | duck to 10 | restore the previous volume on `GAIN` |
| `GAIN` | unduck, then resume if we paused it | — |

**A transient loss caused by our own speech is left alone entirely.** red5's
coordinator already ducks this same mpv for its own clip, and two duckers on one
volume lose the restore between them: on 2026-08-14 at 19:17:38 the app ducked
130 → 10, restored to 130 on the `GAIN`, and the coordinator — which had captured
our ducked 10 as the value to put back — restored to 10 a second later. The music
played quiet for two hours, which is the same symptom `c2db694` fixed and a
different cause. Whoever captured the pre-duck volume puts it back, and for a
spoken reply that is the coordinator, which knows when the whole response ends
rather than one clip.

Telling the two apart is what the speech connection is for beyond the metadata,
and the test is **not** "is speech playing". mpv takes the output when it *opens*
the clip: on p8a the loss landed at 20:16:29 and the first audio at 20:16:40,
eleven seconds later, so that question answers no for a loss that is entirely
ours. The signal is the staging — a new `path`, or the unpause — and a loss
within `STAGING_GRACE_MS` (20 s) of one is ours. Bounded, because for that long
after a clip is staged a genuine outside interruption does not duck; and measured
from the staging rather than the ending, so the grace expires with the reply
instead of being extended by it.

A transient loss is also acted on 300 ms late, for the reverse race — the speech
mpv's state travels a different socket and can arrive just after the callback.
Any newer focus change cancels a deferred one.

`/state` answers this directly: `speech.staged_ms_ago` and
`speech.owns_the_loss`.

Two guards matter more than the table, and both are tested: a resume from
anywhere else (earbuds, CLI, red5) cancels a resume we owe, and a volume we did
not write means something else owns it now — `call_guard` is still live and
ducks the same mpv during calls — so the restore is dropped rather than
clobbering it.

- **`setWillPauseWhenDucked(true)` is not optional.** From API 26 the framework
  ducks the loser's own players itself and does *not* call the listener. Left at
  the default, Android would duck our stream of zeros — a no-op — and mpv would
  play straight through the navigation prompt. The flag buys the callback, not
  an obligation to pause.
- **Duck depth is 10**, which is `InterruptionPolicy.duck_level` for
  `ContentType.MUSIC` in `agent_media_core/route/policy.py` — the level music
  already drops to while Sam speaks over it, not a new number. Deliberately not
  `call_guard`'s 20: keeping them distinct is what makes "did we set this
  volume?" answerable.
- **Probe mode.** A fresh install takes focus but touches mpv for nothing,
  logging every callback; the button on the app's screen switches it to acting,
  and the choice survives a restart. One APK does both because every install
  here is a sideload and a tap through a chooser.
- **Requesting `GAIN` tells other players to stop.** That is the intent, and it
  is the first outward-facing thing this app does.

## What the display says while Sam speaks

One session, whose **metadata** follows whichever channel is in front —
`FrontChannel`. Publishing a second session for speech is the thing not to do:
two sessions compete for the addressed-player slot, which is what the spike
learned and what the transport fix depends on.

| | Music playing | Sam speaking over it |
|---|---|---|
| Title | the track | `Sam` |
| Artist | `agent-media` | the track |
| Duration | the track's | unknown |
| PlaybackState, position, transport | the music mpv | **still the music mpv** |

Only the metadata moves. The `PlaybackState` we publish is what the framework
resolves a `PLAY_PAUSE` toggle from, and answering that question about a
two-second clip is exactly the class of bug `3519172` fixed — so state, position
and every transport callback stay with music. The duration is dropped while
speech is in front because the position beside it is still the music track's.

- **The clip's own title is unusable.** sink-speech plays rendered files, so
  mpv's `media-title` reads `remote-20260814T190922-18480.mp3` — checked against
  the phone's speech mpv, not assumed. Hence the constant. Putting the sentence
  there instead means the speech sink setting `force-media-title` before each
  `loadfile`, which is a red5-side change.
- **Speech in front means *playing*, not *loaded*.** sink-speech keeps the last
  clip open after it ends, and a broker paused from the popup should not hold the
  display.
- **Only `idle-active` and `pause` are observed on the speech connection.** A
  long reply is a clip every few seconds; subscribing to more would flood the
  event log that focus diagnosis is read from.

## Decisions worth knowing

- **The silent track follows `loaded()`, not `playing()`.** It keeps running
  while mpv is paused, and stops when mpv goes idle. Dropping it on pause would
  surrender the addressed-player slot, and the press that would win it back is
  exactly the play button we would then not receive. The battery case that
  actually matters — nothing playing all day — is still covered.
- **`time-pos` is polled, not observed.** mpv fires it continuously; a
  `PlaybackState` carries a position and a speed that the system extrapolates
  between updates. Poll interval is 5 s while playing.
- **`INTERNET` permission is required** even though the only socket is
  loopback. Without it the connection fails with `EACCES`.
- **No `NotificationListenerService`, ever.** Play Protect hard-blocks the
  install of a sideloaded app that declares one, so
  `MediaSessionManager.getActiveSessions()` is unavailable — do not design
  around enumerating sessions.

## Not built yet

Speech *pausing* — the bridge is there and the app reads it, but nothing writes
to it yet; retiring `call_guard` or the Automate mic-detect hold flag; state
*push* to red5 (the pull endpoint above now exists); boot start.

Audio focus is **verified acting on the device**: p8a, 2026-08-14 19:17:38,
`LOSS_TRANSIENT` → duck 130 → 10, `GAIN` at 19:17:56 → restore to 130, with music
playing straight through both (so taking focus does not disturb the pulseaudio
stream mpv plays into — the other long-standing unknown). What that same trace
exposed is the double-duck above, which is fixed but not yet seen fixed on the
device.
