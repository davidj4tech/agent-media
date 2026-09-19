# The companion app, folded into Sasonica

Status: step 1 built 2026-09-19 (sasonica 60e78920): the listener answers on
p8a:6613 and a `media say` through it played. red5 still points at the
companion's 6612; switch with `MEDIA_SPEECH_SOCKET_APP=tcp://p8a:6613`. The
classes live in `com.audiobookshelf.app.speech` (a `com.sasonica` package was
invisible to the Kotlin compile). Proposal written 2026-09-19. Written the day music moved to
Sasonica's player (agent-media 9b4b13e, sasonica ced15c0c). That left the phone
with three apps between it and the rooms: Termux's mpv, the companion (which
speaks) and Sasonica (which plays books and music). This is the plan for
getting down to one. It follows the standing direction from 2026-09-02: *fork Audiobookshelf,
and eventually incorporate the companion*.

## Recommendation in one line

Port about half of the companion into a fork-only package inside Sasonica and
**delete the other half**. Most of the companion exists because mpv ignores
audio focus, and once every channel plays in ExoPlayer or MediaPlayer inside
one app, that problem is gone, not moved.

## Why half of it disappears

The companion's first class says it plainly: *"It plays nothing. The only
audio it opens is a stream of zeros."* `CompanionService` holds a silent
`AudioTrack` so that Android will give it the Bluetooth addressed-player slot,
then drives Termux's mpv over loopback TCP because mpv cannot hold focus
itself. Everything built on that (`FocusPolicy`, `SpeechPolicy`,
`SideChannel`, `FrontChannel`, `ButtonPolicy`, `MpvIpc`, `MpvState`) is a
translation layer between Android's idea of a player and a player that isn't
one.

Sasonica's ExoPlayer is a real player. It already asks for focus
(`setAudioAttributes(…, true)` in `PlayerNotificationService`), publishes the
media session and answers the earbuds. With books and music already there, the
only thing the companion still plays is speech.

## The inventory

53 source files, about 12,000 lines. Each one lands in one of three places.

### Port: the parts nothing else has

These move into `com.sasonica.companion` (fork-only, Java beside Kotlin, which
the Gradle build compiles as it is). Most of them are already free of
`android.*`, and their tests travel with them.

| Files | What it is | Notes |
|---|---|---|
| `MpvServer`, `Json`, `ClipCache`, `BuiltinSpeech` | speech, answering mpv's protocol on 6612 | red5 needs no change: `MEDIA_SPEECH_SOCKET_APP` already points at 6612. Keep MediaPlayer at first (measured: it holds 1.6× exactly); a second ExoPlayer can come later. |
| `MicWatch`, `MicSteady`, `MicSource`, `BargeIn` | is someone talking, or holding the mic for a call? | Plain `AudioManager` recording callbacks, with no permission needed. They carry the mic baseline lessons (Android System Intelligence never lets go of the mic). |
| `DictationHold`, `BookHold`, `HoldRate`, `RingerState` | the hold tier, and whether the phone wants to be spoken to | `BookHold` is rewritten against ExoPlayer directly, which is simpler than before. |
| `Crash`, `ExitReason`, `LastExit` | the app's own record of how it died | Still the only view into a phone with a closed logcat. |
| `StatusServer` | the readout on 8770 | Folded into `SasonicaControl`'s `/state`, with the same fields under a `speech` key, so `media doctor` changes one URL. |
| `ShareActivity`, `ShareRequest`, `Loopback` | "Play with agent-media" in the share sheet | Could go last; the share goes to `media` over HTTP either way. |

### Replace: Sasonica already does it

| Companion | Sasonica |
|---|---|
| `CanvasActivity`, `BackChevron`, `Edges` | `CanvasPanel.vue` (full screen, landscape since f9d9d69b) |
| `RecentActivity`, `RecentList`, `RecentRows` | the Conversations library and `ConversationLog.vue` |
| `AskRequest` | the assistant button and New chat (`POST /ask`) |
| `MainActivity`, `Tabs`, `ChannelCard`, `Artwork`, `CardText`, `Marquee`, `Style`, `Health` | the app's own player UI; a speech card, if one is wanted, is a Vue component |
| `Server`, `Settings`, `SettingsActivity` | a Sasonica settings section, next to Remote control |
| `WakeActivity`, `WakeReceiver` | `SasonicaRemoteService`, the foreground service that already keeps the app thawed |
| `DiagnosticsActivity` | a settings page over the event log, or dropped in favour of `/state` |

### Delete: it existed because mpv ignores focus

`CompanionService` (except the timers and startup the ported parts need),
`FocusControl`, `FocusPolicy`, `SpeechPolicy`, `SideChannel`, `FrontChannel`,
`ButtonPolicy`, `MediaButtonReceiver`, `MpvIpc`, `MpvState`, `Transport`,
`Channels`, `Chapters`, and the `mpv-music-bridge-local` loopback service on
the Termux side.

## The one real design question: two players, one app

Android's audio focus is held per request, not per app. If the speech player
asks for focus, Sasonica's own ExoPlayer loses it and pauses or ducks as if
another app had interrupted. That's roughly the right behaviour by accident,
but it goes through the one mechanism the companion spent a month learning not
to trust.

**Proposal:** inside the app, the channels settle it directly. The
book/music player holds focus against other apps, and speech never asks for
focus of its own. Instead it tells the player, in process, to *duck the
music, pause the speech… pause the book* — David's rule, as written in
`SpeechPolicy`, but as a method call rather than a focus callback. It's
`FocusPolicy` again with the Android plumbing taken out, which is why it's
small.

## Order

Each step ships on its own, and the companion keeps running until the last
one.

1. **Speech in Sasonica, on a new port.** Port `MpvServer`, `BuiltinSpeech`
   and `ClipCache`, and bind them on 6613 so the companion keeps 6612. Try it
   by pointing `MEDIA_SPEECH_SOCKET_APP` at 6613, and move back the same way.
   No arbitration yet, so speech and a book can overlap. This is the proof
   that one app can hold both.
2. **In-process arbitration.** The speech player ducks or pauses ExoPlayer
   directly, and `BookHold` becomes a few lines.
3. **Mic watch and the hold tier.** Port `MicWatch` through `DictationHold`.
   The Termux `call_guard` observe role can then read the app's `/state`
   instead of its own flag.
4. **Readout and share.** Fold `/state` in, and move the share sheet.
5. **Retire the companion.** Uninstall it from p8a, drop `android/companion`
   from this repo's build, and set `MEDIA_SPEECH_SOCKET_APP` to 6612 again,
   now served by Sasonica.

Steps 1 and 2 are most of the value. Once they're done, one app decides who
gets the audio.

## Licensing, and what it does to selling Sasonica

- **Sasonica is GPL-3.0**, inherited from advplyr/audiobookshelf-app, and its
  copyright is shared with every upstream contributor. It can never be
  relicensed as proprietary.
- **agent-media is Apache-2.0**, and the companion is agent-media's. Apache-2.0
  code may be included in a GPL-3.0 work, so the port is allowed. The shipped
  Sasonica, with the companion's code inside it, is then distributed under GPL
  terms. The originals in this repo stay Apache-2.0, and David, as the only
  author, can still license them however he likes elsewhere.
- **What the merge gives up:** today the companion is a separate app talking
  to Sasonica over sockets, and a separate program isn't covered by
  Sasonica's GPL. That process boundary is what lets the companion's features
  ship closed, or be sold separately. Folding them in trades that option for
  one app. Keeping them apart and talking over IPC is the alternative if that
  option matters, and it's the same boundary piper sits behind.
- **Selling a GPL app is allowed**, on the Play Store or anywhere else, but
  every buyer is entitled to the source and may pass it on for free. The
  common pattern is a paid Play listing with free source or F-Droid builds,
  and it earns convenience money, not exclusivity.
- **The revenue that GPL doesn't touch is the service.** agent-media on red5
  (TTS, the conversation library, the assistant) isn't distributed when
  people use it over the network. GPL-3.0 has no network clause (AGPL does),
  and ABS server itself is GPL-3.0, not AGPL. A hosted "Sasonica server" with
  a subscription is the clean commercial shape, and the app becomes its free
  client.
- **Two practical snags for the Play Store,** separate from the license:
  Play's policies on apps watching other apps' recordings (the mic
  detection), and on sideloaded notification listeners (Play Protect has
  already blocked one on p8a). Also, don't use the Audiobookshelf name
  anywhere in store listings; the rename was the right call.

## Decided: how Sasonica earns

2026-09-19. **One app. Free, with an optional supporter purchase or
donations, and the money from a hosted server tier.** No ads: people listen
with the screen off, audio ads would interrupt the book, an ad tracker next to
assistant conversations and mic detection is a trust problem, and the ad SDK
is proprietary code in a GPL app that anyone can strip out. A paid Play
listing works too (Conversations, OsmAnd+), but the value is on the server,
which GPL-3.0 doesn't reach.

## Open questions

- Is keeping the companion's features closed-able worth an extra app?
  Decide before step 3: steps 1 and 2 are mostly glue, and are GPL either
  way.
- MediaPlayer or a second ExoPlayer for speech? Keep MediaPlayer for step 1
  (it's measured), and revisit once the channels share a process.
- Does the Termux mpv stay as the fallback for everything, or only for
  audiobooks that aren't in the app's library (13379 still has only
  Conversations)?
