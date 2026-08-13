# An agent-media Android app for p8a — what it should and should not be

Status: proposal, nothing built. 2026-08-13.

## Recommendation in one line

Build a **small companion app that plays only silence** — it publishes the
Android MediaSession, owns audio focus, and drives the existing Termux mpv over
loopback TCP. Do **not** write a player. mpv stays the engine and remains the
only thing making sound; the app supplies the Android citizenship mpv cannot
have from inside Termux.

> **Verified 2026-08-13.** The spike passed on its third run. The design is
> confirmed with one amendment: the app cannot be audio-free. On Android 17 an
> active session claiming `STATE_PLAYING` does *not* win the Bluetooth
> addressed-player slot — an **open audio stream is required**, and audio focus
> is not a substitute. The shim must hold a silent `AudioTrack` for as long as
> it wants the controls. Consequences to design around:
>
> - **Tie the silent track to mpv's playback**, not to the app's lifetime. A
>   permanently open stream is a permanent wakelock-shaped battery cost and
>   keeps the audio path warm for no reason when nothing is playing.
> - **Two streams now share the output** — mpv's real audio and the shim's
>   silence. Verify this does not disturb routing, A2DP codec negotiation, or
>   the existing duck behaviour.
> - The app is no longer a passive observer, which weakens the "purely
>   additive on day one" claim below. It is still far short of being a player.
>
> See `2026-08-13-mediasession-spike.md` for the full run log.

## Relation to the MPRIS work (session `23090817`, red5)

This proposal is the phone-side half of a pattern that session already built
once, and it should be read as continuing that work rather than as a new idea.

That session exposed the music, speech and book channels over **MPRIS** on
red5 (`route/_mpris.py`; mpv-mpris auto-loading from `/etc/mpv/scripts/mpris.so`,
which is easy to miss because it is not in `ExecStart`), so `playerctl -l` now
lists `mopidy`, `mpv`, `mpv.agent-media-book` and `mpv.mopidy-mpv-music`.

Same problem, two platform bindings: *let the platform's native transport
controls drive our channels, without agent-media pausing itself.* MPRIS is
D-Bus and stops at the machine boundary — it can never reach Android's
Bluetooth stack, so it does not and cannot cover earbuds or the car. A
MediaSession is the Android-side binding of the same idea. Nothing here
duplicates or replaces the MPRIS layer; red5 keeps it unchanged.

Three things carry across:

- **Never pause ourselves, and identify by process, not by name.**
  `_OWN_MPV_MARKERS` exists because mpv-mpris takes a random bus suffix, so the
  only reliable identity is bus name → owner PID → `/proc/<pid>/cmdline`. On
  Android this gets *worse*, not better: `MediaSessionManager` identifies
  sessions by package, and every channel we publish would come from the same
  companion package. Name-based exclusion is meaningless there. The app must
  key on its own `MediaSession.Token` and, like `_mpris.py`, fail closed —
  when it cannot tell whose audio it is, do nothing.
- **Publish one session, not one per channel.** MPRIS can afford three players
  because D-Bus consumers enumerate them. AVRCP cannot: Bluetooth has a single
  *addressed player*, so three sessions would make the car's next-track button
  ambiguous and could address the speech broker instead of music. Publish a
  session for the music channel only; speech and book stay invisible to AVRCP,
  which is the same exclusion `_OWN_MPV_MARKERS` enforces on red5.
- **Do not extend MPRIS to the phone.** The phone has no D-Bus and empty
  `~/.config/mpv/scripts/`; adding `p8a` to `MEDIA_MPRIS_SSH_HOSTS` would not
  reach Bluetooth even if it worked. The MediaSession app is the answer to that
  gap, not a wider MPRIS deployment.

## Why not the alternatives

**Keep the status quo (Termux mpv + tcp:6601).** Cheapest, and everything works
*except* the one thing that cannot be fixed from Termux: no process in the
Termux sandbox can publish a `MediaSession`, so earbud/AVRCP transport buttons
and car-display metadata are permanently dead. That is the whole motivation, so
status quo is only the right answer if David decides those controls do not
matter.

**Write a real player (replace mpv).** This is the expensive option and it buys
little. It would have to re-implement, on Android, the four things that already
work: the residential yt-dlp fetch (yt-dlp is Python and lives in Termux — the
app would end up shelling back into Termux anyway, or swapping to
NewPipeExtractor and inheriting its breakage rate), the `~/.cache/music-offline`
store, ducking under speech, and the mpv JSON IPC contract `SinkMusicLocal`
speaks. Months of work to arrive back where we are, plus MediaSession.

**Drive an existing player instead.** Checked, and the market splits exactly
wrong — the players with a session have no control API, and the thing with a
control API has no session:

| Candidate | MediaSession | Remote control API | Verdict |
|---|---|---|---|
| mpv-android (`is.xyz.mpv`, installed) | Yes — `MPVActivity.kt`, `BackgroundPlaybackService.kt`, `Utils.kt` | No. [PR #187](https://github.com/mpv-android/mpv-android/pull/187) ("optional one-way command server") was **closed unmerged**; [issue #374](https://github.com/mpv-android/mpv-android/issues/374) confirms `input-ipc-server` creates no socket | Intents only (`am start … VIEW`): can start a track, cannot pause, duck, seek, or read state |
| mpvKt (`live.mehiz.mpvkt`, installed) | No `MediaSession` in the repo | No socket/server code | Not a candidate |
| VLC for Android (installed) | Yes | Yes — Remote Access: Ktor REST + WebSocket now-playing push, OTP auth from the notification drawer | Closest fit, but undocumented/unstable surface, OTP auth is human-in-the-loop, and it means abandoning yt-dlp + the offline cache |
| Snapcast Android (`de.badaix.snapcast`, installed) | Yes | Yes, via the server | Deliberately disabled — ~0.7 GB/hr/client, `down` files dated 2026-08-11 |

**Run snapserver *and* snapclient on the phone.** The bandwidth objection to
Snapcast is about the WAN hop from red5; server and client both on p8a makes
the stream loopback, so the ~0.7 GB/hr disappears. Rejected on a hard fact:
**snapdroid publishes no MediaSession** — zero hits for `MediaSession` across
the repo, which is a real negative rather than an indexing artifact (the same
search for `Notification` returns 10, including `SnapclientService.java`). It
shows a plain notification. So the one thing we are trying to buy is exactly
the thing this does not deliver. Secondary cost: Termux packages
`snapcast-client` 0.35.0 only — there is no `snapserver` package, so the server
would have to be built from C++ source on-device.

It is still worth keeping in view, because it solves two *other* real problems
without an app: mpv would output to a pipe rather than an audio device, which
retires the whole OpenSL/pulse fragility, and snapclient is an ordinary Android
app that honours audio focus. That is two of the shim's four jobs. Only the
MediaSession job would remain — and that still needs an app. Also worth
stealing regardless of the decision: Snapcast's documented stream-plugin
control API (`Plugin.Stream.Player.Control`, `doc/json_rpc_api/stream_plugin.md`)
is a working model of exactly the contract the shim's `/state` endpoint needs —
push metadata up, accept transport commands down. Borrow its shape rather than
inventing one.

**Fork mpv-android and re-land PR #187.** Genuinely viable: MediaSession, audio
focus and the `audiotrack` AO all come free. Kept as the fallback if the shim's
session-priority spike fails, but not the first choice: it is a permanent fork
of a large app, the patch is *one-way* (no property reads — `loaded()`,
`position()`, `now_playing_uri()` would all need writing anyway), and the
offline cache would have to move out of Termux's private directory into shared
storage for the app to see it.

Worth knowing before choosing it, though: the feature was **abandoned, not
refused.** In the PR thread `sfan5` observed it would be simpler to do with
intents, the way `NotificationButtonReceiver` already does for the notification
buttons; the author agreed, said he would open a replacement PR, and never did
— it is his only PR on the repo. The diff is small (+141/−1; a 98-line
`CommandServer.kt`) but sits on a 2019-era base.

No fork appears to have shipped it. GitHub code search excludes forks entirely,
so the sweep was done by listing all 455 forks by stargazers and checking the
`is/xyz/mpv` source directory of every fork with a meaningful star count for a
server/IPC/socket/command source file. None has one:

- `aniyomiorg/aniyomi-mpv-lib` (67★) — a libmpv wrapper for Aniyomi, not a player
- `abdallahmehiz/mpv-android` (32★) — the mpvKt author's fork
- `FongMi` (18★), `jakedowns` (9★), `syphyr` (5★), `okcaptain` (4★),
  `Quackdoc` (3★), `khaled-0` (1★) — all clean
- `pepeloni-away/mpv-android` (3★), described as "broader intent support,
  compatible with browsable intents", was the most promising lead. Its default
  branch is upstream master and its manifest still declares
  `NotificationButtonReceiver` as `exported="false"`, so it does not open a
  control surface either.
- the PR author's own fork last pushed 2019

And the intent route upstream signposted does not currently work from Termux:
`NotificationButtonReceiver` is declared `android:exported="false"`, so a
broadcast from another UID cannot reach it. Exporting it (or adding an exported
receiver) is precisely the contribution upstream indicated it would take — but
intents are fire-and-forget, so even the blessed path yields a write-only
control surface with none of the state reads the router depends on.

## The recommended shape

```
red5  ──HTTPS/tailnet──▶  companion app  ──TCP 127.0.0.1:6601──▶  Termux mpv ──pulse──▶ A2DP
                            │  MediaSession  ◀── AVRCP / earbuds / lock screen / car
                            │  AudioFocus    ◀── calls, Cece, nav prompts
                            └──state push──▶ red5 (SSE/webhook)
```

The app does four jobs, none of which is playing audio:

1. **Publishes a MediaSession** with live metadata and `PlaybackState`, and
   maps its transport callbacks onto mpv IPC commands. This is the deliverable.
2. **Owns audio focus.** mpv ignores audio focus, and that single fact is the
   root of most phone-side complexity we carry: the call-guard, the Automate
   mic-detect hold flag, the duck plumbing. The app requests focus on mpv's
   behalf and, on `AUDIOFOCUS_LOSS_TRANSIENT`, ducks or pauses mpv over IPC.
   That retires the Automate dependency, which is already a standing goal
   (`agent-media-native-app-goal`) and has been broken since 2026-08-05.
3. **Exposes one coarse HTTP endpoint to red5** — see below.
4. **Starts on boot**, replacing another Automate/Termux:Boot corner.

### Two hard constraints the design must respect

**The mpv socket is unreachable from another app.** It lives at
`/data/data/com.termux/files/home/.local/state/agent-media/mpv-music.sock`,
inside Termux's private UID sandbox. The companion app cannot open it. It must
talk to mpv over **loopback TCP**, so the mpv-music service needs a second
socat listener on `127.0.0.1:6601` alongside the existing tailnet one. (Sharing
Termux's `sharedUserId` is the alternative and is not worth it.)

**red5→p8a is 891–1171 ms per round trip** (Hetzner→Melbourne). Do not port the
chatty property-at-a-time IPC to HTTP — that just re-creates the problem the
disk-persisted slow-endpoint breaker in `sinks/_mpv_ipc.py` exists to contain.
Two rules:

- **One snapshot call.** `GET /state` returns everything `SinkMusicLocal`
  currently reads (`idle-active`, `pause`, `path`, `time-pos`, `volume`,
  `speed`, title) in a single round trip. Property fan-out happens on-device,
  where it costs microseconds.
- **Invert liveness.** Let the phone *push* state changes to red5 (SSE or
  webhook), so `loaded()` — called on every coordinator duck decision — becomes
  a local cache read. That is what actually kills the breaker class of bug,
  rather than raising `MEDIA_MPV_SLOW_MS` again.

### Scope: wrap, don't replace

Keeps working unchanged: the residential yt-dlp fetch over SSH, the
`~/.cache/music-offline` store, `--ao=pulse`, dynaudnorm, and the mpv JSON IPC
contract. `SinkMusicLocal` can stay on tcp:6601 verbatim through the whole
migration and move to `/state` only when the endpoint proves out — the app is
purely additive on day one.

## Concrete first step

**A one-evening spike that answers the only question that can kill this:** does
a MediaSession published by an app that produces no audio actually become the
Bluetooth *addressed player*?

Android picks the AVRCP target from `MediaSessionManager.getActiveSessions()`,
ordered by playback state and audio focus. There is precedent for silent
sessions working (the desktop `mpris-fakeplayer` trick relies on the same
idea), but it is not guaranteed across OEM Bluetooth stacks, and the whole
proposal rests on it.

The spike: an APK with nothing but a foreground service, a `MediaSessionCompat`
set `STATE_PLAYING` with dummy metadata, and log lines in the transport
callbacks. Sideload it, start Termux mpv playing, then press play/pause and
next on the earbuds and in the car, and check whether the callbacks fire and
whether the car display shows the dummy title.

- **Callbacks fire** → build the shim as described. Roughly: session +
  focus (a weekend), mpv loopback client and metadata sync (a weekend),
  `/state` + push to red5 (a weekend).
- **Callbacks do not fire** → the app must own the audio track, and the
  decision becomes fork-mpv-android vs. status quo. Better to learn that from
  a 100-line APK than from a half-built shim.

## Smaller things found on the way

- `pgrep -a socat` on the phone can show what looks like two listeners on
  `TCP:6601`. It is not a leak: `socat ...,fork` forks a child per accepted
  connection, and the child inherits an **identical cmdline**, so a live IPC
  connection is indistinguishable from a second listener by name alone. Tell
  them apart by parentage — the listener's PPID is its `runsv` supervisor
  (`runsv mpv-music-bridge`), a connection child's PPID is the listener. Killing
  the "duplicate" would drop a live connection, not tidy anything up.
- Termux's mpv 0.41.0 has **no `audiotrack` AO** — `--ao=help` lists only
  pulse/alsa/jack/openal/opensles/null/pcm. mpv-android uses `audiotrack` with
  OpenSL as fallback, which is precisely why it has no silent-playback problem
  and Termux does. `--ao=pulse` is not a workaround to be removed later; it is
  the only correct setting for Termux mpv on this device.
