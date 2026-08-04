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

## Open / parked ideas

- Public / venue listening channels via **Shoutcast** (spas, bath houses) —
  one curated source -> many listeners / zones, layering ambience under
  announcements. Loose idea only, not committed.
