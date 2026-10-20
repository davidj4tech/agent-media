# Picking the phone or TV for audio

Media → Plays on lists connected app players by their paired device names.
The reply player's expanded controls include Speech output, using the same
named device list for speech. The phone's earbuds/speaker switch controls
only its local Android route; it does not choose which device receives audio.

Speech and music have separate choices. A named choice remains selected
when another app connects or reconnects. An offline choice stays selected
and is shown unavailable; audio is not silently sent to another device.
Automatic app player retains the old most-recent-connection behavior.

`GET /audio/targets` and `/music` add `device:<paired-id>` options. A
`POST /audio/target` with that name keeps the underlying sink `sasonica`
and pins that channel's frame destination in `frames-output-<channel>` in
the server state directory. The authenticated device's existing outbound
event stream carries playback; no new inbound phone or TV port is used.

Choosing another device moves the current frame playlist, index, playhead,
speed, volume, mute and pause state. A targeted stop goes only to the old
device, and a targeted snapshot only to the new one. Frames produced while
a named device is selected keep that destination when replayed after a
reconnection, so old clips are not replayed on a newly selected device.
