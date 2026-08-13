# Spike: does a silent MediaSession become the Bluetooth addressed player?

Companion to `2026-08-13-android-companion-app.md`. Nothing built yet — this is
the spec, pending David's go-ahead.

Target device: Pixel 8a (p8a), **Android 17**.

## The question

The companion-app proposal rests entirely on one unverified assumption: that a
`MediaSession` published by an app which **produces no audio** will be chosen by
Android as the Bluetooth *addressed player*, so that earbud and car-stereo
transport buttons deliver callbacks to it and its metadata reaches the car
display — while the actual audio continues to come from Termux mpv.

Android picks the AVRCP target from `MediaSessionManager.getActiveSessions()`,
ordered by playback state and audio focus. Nothing in the API requires the
session owner to be the audio producer, and the desktop `mpris-fakeplayer`
trick relies on the same separation. But it is not guaranteed across OEM
Bluetooth stacks or Android versions, and Android has tightened media
notification and session handling repeatedly since 13.

If the answer is no, the whole shim design collapses and the decision becomes
fork-mpv-android vs. status quo. Learn it from a throwaway APK, not from a
half-built shim.

## Step 0 — zero-code measurement (do this first)

**Attempted 2026-08-13 — abandoned. adb cannot reach p8a from red5, and the
spike has been redesigned to not need it (see "No adb" below).** Findings:

- Wireless Debugging is on, but **adbd binds to wlan0 (192.168.22.16) only**.
  The tailnet address and loopback both give `ConnectionRefused` — from red5
  *and* from the phone itself. red5 is in Germany; 192.168.22.0/24 is David's
  LAN, so there is no route.
- The address Settings displays is misleading: it shows the **Tailscale IP**
  (tun1 shadows wlan0 in Android's address lookup), which is not where adbd is
  listening.
- The persistent port **rotates** — observed 45823 → 33299 within ten minutes —
  and the pairing port only exists while its dialog is on screen.
- Connecting from the phone to its own wlan0 IP times out for *every* port,
  including sshd on 8022, so Android hairpins nothing while the VPN is up.
  That rules out a socat bridge from the tailnet to adbd.
- `adb pair` from red5 fails with `protocol fault (couldn't read status
  message)` — this is just the closed port; the same failure occurs with
  Debian's adb 34.0.5 and Google's official 37.0.1.
- `android-tools` (adb 35.0.2) was installed into Termux during this attempt so
  the phone could pair with itself. That does not work either, for the loopback
  reason above. Harmless to leave, useless to keep.

**Using adb would require a machine on David's LAN.** `pn` was the obvious
candidate and is not currently reachable over ssh.

### No adb: how the spike reports instead

This is a small design change, not a workaround, and it makes the spike
self-contained:

- **Step 0's `dumpsys` snapshot is dropped.** It was a nice-to-have; the
  experiment does not depend on it.
- **The APK sideloads by file.** Copy it to the phone and open it with
  `termux-open`, the way mpvKt and mpv-android got there.
- **The spike logs to shared storage, not logcat.** Each transport callback
  appends a timestamped line to
  `/sdcard/Documents/mediasession-spike.log`, which Termux can read directly
  over ssh — so the results are readable from red5 without adb. Have the app
  also write its session state on start, so a missing file distinguishes "app
  never ran" from "app ran, no callbacks".

- `adb devices` on red5 is empty and `adb connect 100.94.14.59:5555` is refused,
  so nothing is listening; modern Android needs the pair-with-code flow rather
  than a plain port.
- There is **no code-free path from Termux**. `dumpsys media_session` fails with
  `Permission Denial: ... missing android.permission.DUMP` (uid 10002), and
  `cmd media_session list-sessions` returns `***Error listing sessions***` — the
  binary is reachable but the query is gated on `MEDIA_CONTENT_CONTROL`.
- Incidental: `cmd media_session dispatch KEY` exists. If it were reachable it
  would give a *send* path for media keys — the wrong direction for this spike,
  and almost certainly gated the same way, but worth remembering.

`adb` is already installed on red5. Before writing anything:

1. Enable Wireless Debugging on p8a, pair once, `adb connect` from red5.
2. Start music on the phone the normal way; connect the earbuds or car.
3. `adb shell dumpsys media_session`

This costs ten minutes and answers two things on its own: it confirms the
premise (zero active sessions while Termux mpv is audibly playing), and it
shows what the Bluetooth stack currently reports as the addressed player and
what its selection state looks like. Re-run it in step 3 for a direct
before/after. It may also reveal that some other installed app (VLC, Musicolet,
mpv-android) is squatting a stale session, which would change the picture.

## Step 1 — the APK

Roughly 100 lines, one file, no UI beyond a launcher activity that starts the
service.

- A foreground service with `android:foregroundServiceType="mediaPlayback"` and
  the `FOREGROUND_SERVICE_MEDIA_PLAYBACK` permission (mandatory since Android
  14; the service will not start without it).
- A `MediaSessionCompat`, `setActive(true)`, `PlaybackStateCompat` set to
  `STATE_PLAYING` with `ACTION_PLAY_PAUSE | ACTION_SKIP_TO_NEXT |
  ACTION_SKIP_TO_PREVIOUS | ACTION_PAUSE | ACTION_PLAY`.
- `MediaMetadataCompat` with a deliberately recognisable dummy title —
  something like `SPIKE-TITLE-8a` — so there is no ambiguity about whether the
  car display is showing our metadata or something cached.
- A media-style notification tied to the session token.
- `Log.i` in every transport callback, nothing else. It must not play, request
  playback, or touch mpv. **Audio focus is deliberately not requested in this
  step** — see step 4.

**No mpv integration in this spike.** Whether the callbacks fire is independent
of what we would do with them, and wiring IPC in early makes a failure
ambiguous.

## Step 2 — build and install

Neither red5 nor the phone has a JDK, Gradle, or the Android SDK
(`which java gradle sdkmanager` → nothing; only `adb` exists on red5). So the
first concrete cost is a toolchain: JDK 21 + Android command-line tools on
red5, ~2 GB. Gradle's auto-generated debug keystore is fine for signing.

Install either by `adb install` over the wireless-debugging link from step 0,
or by copying the APK to the phone and opening it with `termux-open` and
tapping through. The latter needs no pairing and matches how the other
sideloaded players (mpvKt, mpv-android) got there.

## Step 3 — the test

With Termux mpv audibly playing music through A2DP, and the spike service
running:

1. `adb shell dumpsys media_session` — does our session appear, and is it
   listed as the addressed/active one?
2. Press **play/pause** on the earbuds. Then **next**. Then **previous**.
3. Repeat all three on the car stereo.
4. Check the car display: does it show `SPIKE-TITLE-8a`?
5. Check the phone's lock screen for the media control.
6. `adb logcat -s <tag>` throughout.

Record which of the six surfaces respond, per transport. Partial success is a
real and likely outcome — e.g. lock screen works, earbuds work, car metadata
does not — and it matters, because the car display is the weakest link and the
least essential of the three.

## Step 4 — the follow-on question, only if step 3 passes

Repeat step 3 with the service also holding audio focus
(`AudioManager.requestAudioFocus`, `GAIN`), still producing no audio. Two
things to learn:

- Does holding focus change session priority — i.e. is focus needed to win the
  addressed-player slot, or merely helpful?
- Does grabbing focus **stop or duck the Termux mpv playing underneath**? mpv
  ignores focus, which is the premise of the whole design, but pulseaudio sits
  between them and has not been tested under a focus change. If taking focus
  silences our own music, the focus half of the proposal needs rethinking even
  though the MediaSession half works.

## Result of run 1 (2026-08-13, ~21:33–21:38)

APK built without Gradle (`spikes/mediasession/`, 16 KB, platform APIs only)
and sideloaded via `termux-open`. The service came up correctly —
`21:33:16 service started; session active, state=PLAYING, no audio`.

**No transport callbacks and no media-button events fired at all**, from either
the earbuds or the car, both without audio focus and while holding it
(`audio focus requested -> GRANTED` at 21:33:21, 21:34:36, 21:36:53).

Two observations that stop this being a clean negative:

- **The buttons did something.** At `21:37:44` the log records
  `focus change: -2` (`AUDIOFOCUS_LOSS_TRANSIENT`) followed by
  `focus change: 1` (`AUDIOFOCUS_GAIN`) thirteen seconds later. Something else
  took focus transiently during the test. So the press was delivered — to
  another owner. A transient loss of that shape is also the classic signature
  of a voice assistant waking, which is what a long earbud press often does
  instead of sending a media key. The car's dedicated next/prev buttons send
  unambiguous AVRCP and are the better probe.
- **The spike never set a media button receiver.** `MediaSession` has
  historically needed `setMediaButtonReceiver(PendingIntent)` for buttons to
  reach a session while the app is backgrounded. Run 1 omitted it. That is a
  plausible cause of total silence and is cheap to correct.

### Run 2 — what to change before concluding anything

1. Add `setMediaButtonReceiver` with an exported `BroadcastReceiver`, and log
   what arrives there separately from the session callbacks.
2. Add a `NotificationListenerService`. With notification-listener access
   granted, the app can call `MediaSessionManager.getActiveSessions()` and
   **name the package currently holding the addressed-player slot** — the
   measurement `dumpsys` would have given us, obtained legitimately from
   inside the app rather than over adb.
3. Re-test with the car's discrete next/previous buttons rather than a long
   earbud press, to avoid the assistant confound.

Only if run 2 is also silent does the shim design fail.

## Result of run 2 (2026-08-13, ~21:55)

Two findings.

**Play Protect blocks session enumeration outright.** The first run-2 build
declared a `NotificationListenerService` to unlock
`MediaSessionManager.getActiveSessions()`. Play Protect refused to install it —
"App blocked to protect your device… can request access to sensitive data" —
with only a "Got it" button and no install-anyway path. This is a standing
constraint, not a one-off: **a sideloaded build cannot enumerate media
sessions**, so "who holds the addressed-player slot" is unanswerable without
either Play Store distribution or disabling Play Protect. The listener was
dropped and the build re-sent as `spike-run2b.apk`.

**With the media-button receiver wired, still nothing.** Neither the car
stereo's discrete transport buttons nor the earbuds produced a `RECEIVER` line
or a session callback. That removes the leading explanation for run 1.

## Run 3 — the last cheap variable: play actual silence

One untested difference remains between the spike and a real player, and it is
not audio focus: the spike never opens an **audio stream**. Android's
addressed-player selection may require an active `AudioTrack`, not merely an
active session with `STATE_PLAYING`.

The change is small — a looping silent PCM `AudioTrack` at low volume, started
with the session. Apps that exist to proxy transport controls commonly do
exactly this. It would also make the audio-focus behaviour honest rather than
notional.

If run 3 is silent too, the silent-shim design is dead and the recommendation
moves to forking mpv-android (see the companion proposal). Note that a shim
playing real silence is a meaningfully different animal from the one proposed:
it holds an audio stream permanently, with the battery and audio-routing
consequences that implies.

## Result of run 3 — PASS (2026-08-13)

**The silent `AudioTrack` was the missing piece.** With an active audio stream
writing zeros, the transport controls reached the app. Runs 1 and 2 had failed
for the same reason, and audio focus was never the variable that mattered.

The finding, stated precisely: on Android 17, an active `MediaSession` claiming
`STATE_PLAYING` is **not** sufficient to win the Bluetooth addressed-player
slot. An open audio stream is required. A session without one is invisible to
AVRCP no matter what its playback state says and no matter whether it holds
audio focus.

The kill question is answered and the companion-app design survives — with one
amendment: **the shim is not audio-free.** It must hold a silent `AudioTrack`
for as long as it wants the controls. See the companion proposal for what that
changes.

**Metadata confirmed too**: the car display shows `SPIKE-TITLE-8a`. Transport
control and metadata travel separate paths and both work, so the spike is a
full pass — the car gets our buttons *and* our track information from an app
that emits nothing but zeros.

## Outcomes

| Result | Decision |
|---|---|
| Callbacks fire, metadata reaches the car | Build the shim. ~3 weekends: session + focus, mpv loopback client + metadata sync, `/state` + push to red5 |
| Callbacks fire, car metadata does not | Still build it — transport control is the point; note the car display as a known gap |
| No callbacks even with audio focus | Shim is dead. Reopen fork-mpv-android vs. status quo, with the +141/−1 PR #187 patch as the starting point |
| Focus grab silences Termux mpv | MediaSession half proceeds; drop or redesign the focus/Automate-replacement half |

## Cost

Half a day for steps 0–3, plus the ~2 GB toolchain install. The APK is
throwaway; none of its code is intended to survive into the shim.
