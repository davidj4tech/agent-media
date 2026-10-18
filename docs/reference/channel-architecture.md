# agent-media — channel architecture & player notes

_Last updated: 2026-08-04_

## Multi-channel audio model

Audio is split into channels, each driven by the backend that suits it best —
players are NOT rivals, they're control surfaces landing on different channels.

| Channel | Backend | Notes |
|---|---|---|
| Bulk        | MPV | via mpv JSON-IPC |
| Speech      | MPV | ducking target during Claude voice chats |
| Local player| MPV | local files, incl. local music |
| Music       | MPD | dedicated music daemon, always running |

## Control surfaces (players)

- **listen.el** (Emacs, alphapapa) -> **mpv backend** (preferred/default as of
  listen v0.10; auto-detected over VLC for more robust IPC). Warm Emacs client =
  near-free startup. Drives the mpv-side channels.
  - Key elisp entry points: listen-queue-add-tracks, listen-queue-play,
    listen-queue-new. Call via emacsclient -e from red5.
  - **Not yet installed on red5** (featurep/locate-library both nil, 2026-08-04).
    TODO: package-install + set listen-backend to mpv, then have Emacs report
    exact non-interactive arglists.
- **EMMS** (Emacs Multimedia System, ~20yr old) -> **over MPD** for the music
  channel. Mature/comprehensive vs listen's lean/modern. Its native caching is
  METADATA only — not audio. (Audio caching already handled at the yt-dlp layer.)

## tmux / TUI controls (backend-agnostic)

- now-playing in tmux status line via playerctl metadata / mpc current
- transport keybindings in tmux.conf -> playerctl / mpc
- Works regardless of chosen player, as long as it speaks MPRIS or MPD.

## YouTube is fetched by the phone, not the server

YouTube blocks downloads from data-centre IP ranges, so a server on a VPS
cannot fetch a clip for itself (David, 3 Oct 2026). The `sasonica` music
target (`sinks/music_sasonica.py`) therefore has the phone download it
(`play-local --fetch-only`, skipped when the phone already has it) and hands
the app a loopback URL on the phone's music-files service
(`MEDIA_MUSIC_SASONICA_FILES`, default `http://localhost:6616`). Downloading
on the server and serving it over a tunnel is not an option however fast the
tunnel is. The server-side route the `abs` target uses (`music_app.resolve`)
is only the fallback when the phone has no copy, and it is slow over a
mobile link (~8 KB/s measured, 28 Sep 2026).

Any new YouTube feature has the same constraint. Only *how* the server asks
the phone to fetch may change (today over ssh to the phone; off the tailnet,
the phone would take the job over its own outbound connection).

## Open / parked ideas

- Public / venue listening channels via **Shoutcast** (spas, bath houses) —
  one curated source -> many listeners / zones, layering ambience under
  announcements. Loose idea only, not committed.
