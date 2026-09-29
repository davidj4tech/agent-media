# Music that is licensed: the radio beyond yt-dlp

Status: proposal, nothing built.
Date: 2026-09-29

David, 29 Sep 2026, with the first DJ station playing: *"So how do we get
around copyright issues with all of this?"* The answer given: we don't get
around them — the YouTube path stays personal, and the product plays through
services that hold the licences. Then *"both"*: write this up, and look at
what Spotify allows.

Not legal advice; a map of where the lines are.

## Where it stands

Every song the radio and the DJ play is downloaded from YouTube by yt-dlp on
the phone (play-local, `music_sasonica`, `radio.py`).

- **Copyright.** Downloading commercial recordings one has not bought is
  outside Australia's personal-use exceptions; format-shifting (Copyright Act
  s109A) covers copies of recordings you own.
- **YouTube's terms** forbid downloading other than by YouTube's own features,
  and getting past its stream protection is what the RIAA's 2020 takedown of
  youtube-dl leaned on (anti-circumvention).
- **For one person on his own server** the realistic cost is a blocked account
  or IP (red5 is already bot-walled). **For a product** — accounts, an
  installer, `com.sasonica.app` — offering it to others is infringement at
  scale, Google Play rejects apps that download from YouTube, and it is the
  kind of thing that brings a small company a takedown or worse.

So: the YouTube path stays, **personal and self-hosted only** — off by default
in anything shipped, never on sasonica.com, documented as "your own server,
your own risk". Everything below is for everyone else.

## What was looked at (29 Sep 2026)

| Route | Licensed | Background / pocket | Who can use it | Verdict |
|---|---|---|---|---|
| YouTube IFrame player | yes | **no** — the developer policies forbid background play and audio-only | anyone | not a radio |
| Spotify App Remote SDK (Android) | yes | yes (the Spotify app plays) | **5 hand-listed users** in development mode, the owner on Premium; extended access since 15 May 2025 only for organisations with ≥250k monthly users | David's own, not the product's |
| Spotify Web API (search, queue) | yes | — | same quota | same |
| Apple MusicKit for Android | yes | yes (its own player) | Apple Music subscribers; Apple Developer Program | possible, one service |
| **Android's media hand-off** (below) | yes — the listener's own app and subscription | yes | anyone with any music app | **the product's route** |
| The listener's own files / Audiobookshelf | yes (their copies) | yes | anyone | yes, as a source beside the others |

Spotify's App Remote needs `canPlayOnDemand` (Premium) to play a single
track, and can play and queue a URI and read the player's state — everything
a station needs — but the quota makes it a personal integration only.

## The product's route: Sasonica as the DJ, the listener's app as the player

Android already lets one app ask another to play something by name, and lets
an app with the listener's permission see and steer what another is playing:

- **Play:** `MediaStore.INTENT_ACTION_MEDIA_PLAY_FROM_SEARCH` with the artist
  and title (the Assistant's "play X on Y"), sent to the app the listener
  chose — Spotify, YouTube Music, Apple Music, Deezer, Tidal, a local player.
  Their subscription, their licence, their catalogue.
- **Follow:** `MediaSessionManager.getActiveSessions` (a notification-listener
  permission, asked once) gives that app's `MediaController`: what is playing,
  where it is, when it ends — so the station knows when to hand over the next
  song — and pause/play/skip for the Media tab and the lock screen.
- **Speech** needs nothing new: Sasonica's speech takes transient audio focus
  and every music app pauses or ducks for it, as the app's own player does now.

The station code barely changes. `radio.py` keeps the list, 👎, likes, the
DJ's picks and Up next; only the source (`mix` / `radio_dj.search`) and the
player (`_send`, `_props`) sit behind a new seam:

- **source**: YouTube's Mix (personal), the DJ's "Artist - Title" lines (as
  they already are — no YouTube needed to *pick*), later Spotify's or Apple's
  recommendations where the listener has them;
- **player**: the phone's own players via yt-dlp (personal), or **hand-off**
  to the listener's app.

The DJ is the natural fit: it already thinks in "Artist - Title", which is
exactly what a play-from-search takes.

### Costs of the hand-off

- A song starts ~1-2 s after the last ends (no gapless queue in someone
  else's app); fine for a radio, not for a live album.
- What the other app finds for a search is its choice (a live version, a
  cover); the station reads back what actually played and records that.
- Android only. iOS has no equivalent for third-party apps (MusicKit there
  would be its own work).
- The notification-listener permission is a scary-sounding prompt; the app
  says why before asking.

## Order

1. **Seam** in `radio.py`: source and player as two small interfaces; today's
   behaviour becomes `youtube` + `phone-player`, unchanged.
2. **Gate** the YouTube path: a server setting (`MEDIA_RADIO_YOUTUBE=1` on
   red5, off by default), and the app hides 📻 from a YouTube track when the
   server says it is off.
3. **Hand-off player** in the app (Kotlin/Java beside `Media3Music`): send a
   play-from-search, follow the controller, report to the server over the same
   IPC the station already reads (`path` → the other app's media id,
   `time-pos`, `duration`, `idle-active`), so `tick()` is unchanged.
4. **Settings → Music app**: which installed app plays (the ones that answer
   play-from-search), and the permission.
5. **Spotify for David** (optional): App Remote in development mode, David
   on the allowlist — gapless queueing and exact track URIs, for one.

## Open

- Whether the DJ should see the listener's app's own recommendations (Spotify
  and Apple have them) or stay model-only.
- Offline: a hand-off station needs the other app's own downloads.

Sources: developer.spotify.com (quota modes; "Updating the Criteria for Web
API Extended Access", 15 Apr 2025); spotify.github.io/android-sdk (App Remote
PlayerApi); developers.google.com/youtube/terms (developer policies);
developer.apple.com/musickit/android.
