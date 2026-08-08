# Proposal: `media play-video` subcommand

Status: **proposed**
Date: 2026-08-07

## Motivation
Launching a local video on the phone currently requires a hand-crafted
`am start` intent, ssh'd into the phone (p8ar). The default video handler
is flaky (spinning wheel, lost when switching apps). Pinning the launch to
a specific player (VLC / mpv) is reliable. This should be one clean command
from red5, living alongside the music channels.

## Key facts learned (2026-08-07)
- Commands must `ssh p8ar` first — bare relay commands run on red5, not the phone.
- Phone media path example:
  /storage/emulated/0/Download/yoga/<file>.mp4
- Players installed on phone:
  - VLC:   org.videolan.vlc   (reliable; used successfully)
  - mpvKt: live.mehiz.mpvkt
  - mpv:   is.xyz.mpv
- Working launch shape (VLC component):
  ssh p8ar 'am start -n org.videolan.vlc/.gui.video.VideoPlayerActivity \
    -a android.intent.action.VIEW -d "file:///<path>" -t "video/*"'

## Proposed CLI
  media play-video <name-or-fuzzy> [--player vlc|mpvkt|mpv]

- <name-or-fuzzy>: match against a video dir (default Download, recurse),
  fuzzy substring match like music search.
- --player: default vlc. Map alias -> package/component.
- Wrap ssh-into-phone + am start intent; report resolved file + player.

## Open items
- Confirm mpvKt / mpv launch component names (VLC's is
  .gui.video.VideoPlayerActivity; others TBD).
- Decide search roots (Download, Movies, DCIM?).
- Optional: resume-position handling.
