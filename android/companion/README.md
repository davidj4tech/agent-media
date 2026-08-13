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
| `CompanionService.java` | Session, notification, the silent `AudioTrack`, and the wiring in both directions. |
| `MainActivity.java` | The readout — state and an event log on screen, because there is no adb. |
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

Requires a socat listener on `127.0.0.1:6601` into mpv's IPC socket: mpv's
socket lives inside `com.termux`'s private UID sandbox and no other app can
open it. That is a **separate** runit service from `mpv-music-bridge`, which
binds the Tailscale address only and must not be touched. Same port number is
fine — different bind address.

Install: `scp` the APK to `~/storage/downloads/` on the phone, then
`termux-open --chooser <file>`. adb cannot reach p8a from red5 (adbd binds
wlan0 only).

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

Audio focus (and with it the retirement of the Automate mic-detect hold flag),
the `/state` endpoint and state push to red5, and boot start.
