# agent-media companion (Android, p8a)

Publishes **one MediaSession per channel** — music, speech, book — so each gets
its own card in the shade's media player, and drives the Termux mpv underneath
each one over **loopback TCP**. Music is the phone's *player*: it alone holds the
silent stream, the audio focus and the media-button receiver, so earbud,
lock-screen and car-stereo transport still reach it and only it.

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
| `FrontChannel.java` | Whose focus loss a spoken clip is — the hard question, and since 2026-08-15 the only one this class answers. `android.*`-free, so `test/run.sh` covers it. |
| `SideChannel.java` | A channel with a card but no claim on the phone: session, notification, transport, IPC for speech and book. |
| `FocusPolicy.java` | The audio-focus decision table for **music** — duck, restore, and what is owed back afterwards. `android.*`-free, so `test/run.sh` covers it. |
| `SpeechPolicy.java` | The same for **speech**: pause, resume, and the deadline that stops a pause being stranded on the broker. Also `android.*`-free. |
| `BookHold.java` | And for the **book**, on the one route that reaches it: stop for a conversation, pick it back up afterwards — but never after a call. `android.*`-free. |
| `FocusControl.java` | The `android.*` half of focus: request, abandon, forward the callbacks. Also the tripwire on `FocusPolicy`'s duplicated constants. |
| `CompanionService.java` | Session, notification, the silent `AudioTrack`, and the wiring in both directions. |
| `StatusServer.java` | The readout the outside can reach: `/state` and `/log` over loopback HTTP. `android.*`-free, so `test/run.sh` covers it. |
| `MainActivity.java` | Home: the channel tabs, the one being driven with its transport, and the health strip. |
| `Tabs.java` | The channel selector: a tab each, underlined in its hue, with a dot when that channel is playing. |
| `DiagnosticsActivity.java` | The event log, the focus probe/acting switch and the exit history — where the old main screen went. |
| `ChannelCard.java` | One channel, drawn the same in the shade and in the app. Row and driver are the same component at two sizes. |
| `Transport.java` | The verbs a media card has no room for: seek by an amount, speed, volume, mute, chapters. |
| `Style.java` | The colours, type sizes and spacing every surface shares. There is no theme to hang them on. |
| `Artwork.java` | The per-channel mark and tile, drawn in code — no res/drawable, no font dependency. |
| `RecentRows.java` | Day breaks, clock times, and the titles that are not titles. `android.*`-free. |
| `Health.java` | The home screen's verdicts: mic watch, bridges, deaths today. `android.*`-free. |
| `CardText.java` | The second line of a card: artist, queue depth, time left. `android.*`-free. |
| `WakeActivity.java` | The revive door `call_guard` knocks on after a kill. No window: starts the service and finishes. |
| `MediaButtonReceiver.java` | Logs the broadcast path. Handles nothing; exists for diagnosis. |
| `ShareActivity.java` | "Play with agent-media" in the share sheet. Draws nothing, finishes in `onCreate`, toasts the verdict. |
| `ShareRequest.java` | The pipe that carries shared text to Termux, and the answer back into one line. `android.*`-free, so `test/run.sh` covers it. |

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

Requires three socat listeners on loopback, one per mpv IPC socket: mpv's
sockets live inside `com.termux`'s private UID sandbox and no other app can open
them. A missing one costs exactly its own card — `MpvIpc` reconnects with
backoff forever, so the card appears when the bridge does.

| Service (`packages/core/services/`) | Listener | Socket | What it carries |
|---|---|---|---|
| `mpv-music-bridge-local` | `127.0.0.1:6601` | `mpv-music.sock` | transport, focus actions, metadata |
| `mpv-speech-bridge-local` | `127.0.0.1:6602` | `sink-speech.sock` | is a clip playing, the coordinator's speaking flag, the focus pause, and the speech card |
| `mpv-book-bridge-local` | `127.0.0.1:6603` | `sink-book.sock` | the book card, and nothing else |

A fourth service, `media-share`, is not a bridge: it is the doorway the share
sheet knocks on.

| Service | Listener | What it does |
|---|---|---|
| `media-share` | `127.0.0.1:8771` | `POST /share` — classify a shared link and play it on the channel that fits |

It runs `media` in Termux, which is the point: `ShareActivity` cannot, because
`media`, mpv and yt-dlp all live inside `com.termux`'s UID. Unlike the bridges
and the app's own `/state` readout, this endpoint *starts playback*, so the
loopback bind is a harder rule here than anywhere else — anything that can
reach it can drive the speakers.

Both bridge sets are **separate** services from dotfiles' `mpv-music-bridge` /
`mpv-speech-bridge`, which bind the Tailscale address only and must not be
touched. Same port numbers are fine — different bind address. p8a wires the two
local ones up as whole-dir symlinks (`source: link` in
`ansible/host_vars/p8a.yml`).

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
`focus_mode` (probe or acting), `focus_held`, `owes_unduck`, `restore_volume`,
`speech.owes_resume`, and `focus_events` — every focus callback the app has seen,
timestamped.

## Three cards, one player

Each channel publishes its own `MediaSession` and its own `MediaStyle`
notification, which is what the shade's media player is actually built from —
sessions alone do not produce a card. So three ids: 1 (music, and the
foreground-service notification), 2 (speech), 3 (book).

**Only music opens an `AudioTrack`.** That is the rule the third session rests
on, and it is the spike's own finding read the right way round: a session with
no open stream does not get the Bluetooth addressed-player slot. The stream of
zeros exists to win that slot, so the channels that must never take it simply
never open one. Speech and book also get no media-button receiver, no audio
focus, and no skip/seek actions.

A card is shown only while its channel has something to show, and the two
differ: **a book stays while it is loaded**, because a book is a thing you come
back to tomorrow, while **the speech card follows the reply** — sink-speech
parks the last clip open indefinitely, so "loaded" would leave Sam sitting in
the shade all evening. The speech card also outlives its own pause button, or
there would be nothing left to resume him with.

This retired the front-channel mechanism, which existed only because one card
had to describe several channels by taking turns. In one morning that produced a
card titled `Sam` reporting `STOPPED` whose play button drove an idle music mpv,
and then a card that could pause Sam but not resume him. Each channel names
itself now, and the music card is a music card again — which also means its
pause button works *while* Sam is talking, something one shared card could never
offer.

## Audio focus

The app holds audio focus **on mpv's behalf** — mpv ignores it, which is the
root of most of the phone-side complexity agent-media carries. It is held while
*either* channel has something open, and there are two different claims:

| While | Claim | Because |
|---|---|---|
| music `loaded()` | `AUDIOFOCUS_GAIN` | mpv is the phone's player and owns the output. Held while paused too — abandoning it on our own pause would forfeit the `GAIN` that says to resume |
| a speech clip is playing, or a speech pause of ours is outstanding | `AUDIOFOCUS_GAIN_TRANSIENT` | a reply is two seconds of borrowing. `GAIN` would stop the listener's podcast for good, and Android would never start it again |

Music wins the tie, so a clip spoken over our own track does not downgrade a
permanent claim to a transient one mid-track.

**Speech had no claim at all until 2026-08-15, and that made the speech half
unreachable.** Focus was requested for `state.loaded()` — the music mpv alone —
so a spoken reply with no music behind it left the app holding nothing. David
played YouTube over Sam at 08:10 and *no callback arrived*: you cannot be told
you lost what you never took. The interruption that lands mid-sentence is the
whole point of the speech half, and it is exactly the case where music is least
likely to be playing.

The third term — an outstanding speech pause — is there for the same reason:
dropping focus while one is owed would forfeit the `GAIN` that pays it, and the
pause would stand until the deadline discarded the clip.

**A permanent `LOSS` kills the request, and the app has to agree.** The
`AudioFocusRequest` we registered is dead after an `AUDIOFOCUS_LOSS` — nothing
more will ever arrive through it — so `FocusControl.lost()` drops the
bookkeeping to `NONE`. Without that the app believed it still held focus and
never asked again: on 2026-08-15 the YouTube app took the output at 09:09:01 and
every later interruption arrived at nobody, with `/state` reporting
`focus_held: true` and `focus_events` frozen on that one line.

Re-requesting is then deliberately *delayed*, because taking it straight back
would stop the video David just started, one poll after getting out of its way.
The app asks again when it has something to play — `state.playing()` or speech
audible — not when it is merely owed something. A paused clip and its
outstanding resume are reasons to **keep** focus, never to take it. `/state`
shows this as `focus_lost`.

David's rule is **duck the music, pause the speech**, and both halves are now
live: `FocusPolicy` drives the music mpv on 6601, `SpeechPolicy` the speech mpv
on 6602. They are separate classes rather than one table with a flag because the
two channels fail in opposite directions — music ducked under a navigation
prompt is still music, while speech ducked under one is *gone*: the words keep
going, nobody hears them, and nothing replays them.

| Focus change | Music | Speech | Owed afterwards |
|---|---|---|---|
| `LOSS` | restore volume, then pause | pause | music: nothing — a permanent loss is not followed by a resume, and music restarting minutes later is worse than a button press. Speech: **the resume is still owed** (below) |
| `LOSS_TRANSIENT` | duck to 10 | pause | restore / resume on `GAIN` |
| `LOSS_TRANSIENT_CAN_DUCK` | duck to 10 | pause — permission to duck is not permission to be inaudible | restore / resume on `GAIN` |
| `GAIN` | unduck | resume — but only if the interruption was short (below) | — |

**Whether a `GAIN` resumes the sentence depends on how long it was gone.**
David's rule, 2026-08-15: *depends how long it was paused for; for short
interruptions, make it resume.*

| How long the pause stood | What happens on the `GAIN` |
|---|---|
| under `RESUME_WINDOW_MS` (30 s) | resume — the sentence picks up where it stopped. A navigation prompt or a notification chime is always in here |
| over it | **nothing** — the pause stands. A voice resuming mid-clause after a call is startling rather than helpful, so lifting it is David's (popup Space, `media resume`). This is the policy `call_guard` chose for calls, kept |
| over `RESUME_DEADLINE_MS` (5 min) | the clip is **discarded** — `stop`, then clear the pause |

**A permanent loss never sends a `GAIN`, so the window would never fire for the
apps that matter most.** The YouTube app claimed the output with
`AUDIOFOCUS_GAIN` on p8a at 08:58:19 — a real media app takes focus for good,
not on loan. Android says nothing when it stops. So while a pause of ours is
outstanding the app *listens* instead: `AudioManager.isMusicActive()` going
false means whatever took the output has stopped using it, and that feeds the
same `GAIN` branch — same two-minute window, same manual-resume rule.

That is a heuristic where the callback is a fact, so it is bounded three ways:
only while a pause is owed, only while our own music mpv is idle (otherwise we
are hearing ourselves), and only after two consecutive quiet polls (~10 s), so
an app that takes focus and pauses for breath is not talked over. **The silent
`AudioTrack` stops while listening** — our zeros are a player like any other and
`isMusicActive()` counts them, so leaving it running would answer "someone is
playing" forever. To hear whether anyone else is making a noise, stop making
one yourself. The cost is the addressed-player slot for the length of the
interruption, which is time we are not the player anyway.

While our own music is loaded — even paused — the app does not listen at all,
because the answer would be about us. That case falls through to the deadline.

That last row is not a third policy, it is the one thing neither of the others
can be allowed to leave behind. mpv's `pause` is a property of the player, not
of the clip: it outlives the file that was open when it was set. A stranded
music pause costs a button press; a stranded speech pause costs *every later
reply*, which loads into the paused broker and plays silently — and there is no
button on the speech broker. So the debt survives the clip ending, and past the
deadline the app drops the clip and hands the broker back idle rather than
either resuming a five-minute-old half sentence or forgetting the pause exists.
The position poll is what ticks it. The coordinator clearing `pause` at the
start of each response (`sinks/speech.py`, `reset_state`) is the backstop
underneath all of it, and the one that covers the app being killed mid-pause.

**The speech pause is owed back after a permanent loss and the music pause is
not**, for the same reason: `LOSS` is where a forgotten speech pause is most
likely, since no `GAIN` is coming by definition.

**A transient loss caused by our own speech is left alone entirely — both
halves.** For music, because red5's coordinator is already ducking that mpv (see
below). For speech, because mpv takes the output when it *opens* the clip: acting
on that loss would pause the very clip that produced it, which is Sam silencing
himself on the first word of every reply.

The cost of trusting the flag is that a *genuine* interruption arriving while a
reply is in flight is read as ours, so it neither ducks the music nor pauses the
speech. That is inherent — the flag says a response is in flight, not which
player took the output — and it is the same trade the music duck already makes.
red5's
coordinator already ducks this same mpv for its own clip, and two duckers on one
volume lose the restore between them: on 2026-08-14 at 19:17:38 the app ducked
130 → 10, restored to 130 on the `GAIN`, and the coordinator — which had captured
our ducked 10 as the value to put back — restored to 10 a second later. The music
played quiet for two hours, which is the same symptom `c2db694` fixed and a
different cause. Whoever captured the pre-duck volume puts it back, and for a
spoken reply that is the coordinator, which knows when the whole response ends
rather than one clip.

Telling the two apart is what the speech connection is for beyond the metadata,
and **the coordinator says so rather than the app guessing**. It sets
`user-data/agent-media/speaking` on the speech mpv for the length of a response —
raised in `pre_pause_remote`, before rendering, and lowered in `after_speech`.
mpv's `user-data` is observable and arbitrary, so this needs no new channel and
no script; the app subscribes to it alongside the other three properties.

Watching playback cannot answer the question, and that is not a tuning problem.
mpv takes the output when it *opens* a clip, and a response is rendered and
relayed ahead of time: on p8a the loss arrived at 20:16:29 with audio at 20:16:40
(11 s), and again at 20:26:52 with the clip staged at 20:27:29 — **37 s**. A
window narrow enough to be useful cannot catch that, and one wide enough stops
ducking real interruptions.

Two fallbacks remain, for a coordinator too old to set the flag: speech audibly
playing, and a loss within `STAGING_GRACE_MS` (20 s) of a clip being staged (a
new `path`, or the unpause). Measured from the staging rather than the ending, so
the grace expires with the reply instead of being extended by it — and never from
"a file is loaded", since sink-speech parks the last clip open indefinitely.

The flag is believed for at most `SPEAKING_FLAG_MAX_MS` (5 min). It is cleared in
`after_speech`, so a process killed mid-response leaves it raised, and a raised
flag means never ducking for anything.

A transient loss is also acted on 300 ms late, for the reverse race — the speech
mpv's state travels a different socket and can arrive just after the callback.
Any newer focus change cancels a deferred one.

`/state` answers this directly: `speech.speaking`, `speech.speaking_ms_ago`,
`speech.staged_ms_ago`, `speech.owns_the_loss`, and `speech.owes_resume` — the
one pause on this phone that nothing else will undo.

Two guards matter more than the table, and both are tested. A volume we did not
write means something else owns it now — `call_guard` is still live and ducks the
same mpv during calls — so the restore is dropped rather than clobbering it. And
a resume from anywhere else (the popup, the CLI, the coordinator starting the
next response) cancels a speech resume we owe, for the same reason: whoever
resumed it owns the pause now.

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
- **An app that never asks for focus is invisible to all of this.** Firefox
  playing a YouTube video on p8a on 2026-08-15 produced *no callback at all* —
  checked against the log, which records every callback before any decision is
  taken — and so played straight over Sam. There is no signal to act on: the
  same sin mpv commits, and the reason this app exists. `isMusicActive()` cannot
  separate it from our own speech either, since both are live at once. Detecting
  it would mean `registerAudioPlaybackCallback` and counting active players,
  which is a heuristic on top of a heuristic; not built.
- **Requesting `GAIN` tells other players to stop.** That is the intent for
  music, and it is the first outward-facing thing this app does. Speech asks for
  less on purpose — see the two claims above.

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
| PlaybackState | the music mpv | the speech mpv |
| Play / pause / stop | the music mpv | the speech mpv |
| Next / previous / seek | the music mpv | **not offered** — a clip has no next |

**The card describes one channel, and its buttons drive that same channel.**
Until 2026-08-15 the `PlaybackState` and every transport callback stayed with
music, on the reasoning that resolving a `PLAY_PAUSE` toggle against a
two-second clip is the class of bug `3519172` fixed. That was half right and the
wrong half in practice: while Sam spoke with no track open, the card said
`STOPPED` under a title that said `Sam`, its button showed a play triangle, and
pressing it sent `pause=false` to an idle music mpv. David pressed it five times
in a row at 08:22 before a `previous` finally loaded a track. A control labelled
with one channel and wired to another is worse than a stale toggle.

Pausing Sam mid-reply is also a thing to want, and it is the action the button
most obviously offers while he is the one talking. Next, previous and seek are
withdrawn rather than pointed at the music underneath — a clip has none of them.
The duration is still dropped while speech is in front, because the position
beside it is not a clip position we track.

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
