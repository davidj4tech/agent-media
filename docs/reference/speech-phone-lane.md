# Speech on the phone: the lane and its diagnostics

When speech plays in the Sasonica app, the server does not dial the phone's
player for each command. Every caller (`sinks/speech.py`, the follow loop in
`intake/submit.py`, the `media` keys) still speaks mpv JSON-IPC to a local
port, and what answers depends on the app build. How the phone renders a
reply itself is in
[speech-phone-render-grade-b.md](speech-phone-render-grade-b.md); this page
is the transport and what to read when it goes wrong.

## Frames (current apps)

`packages/server/src/agent_media_server/speech_frames.py`. The canvas
listens on `MEDIA_SPEECH_FRAMES_LISTEN` (e.g. `127.0.0.1:16624`), and
`MEDIA_SPEECH_SOCKET_SASONICA` pointed at it is the switch.

- **Commands that change the player become frames.** Each command is turned
  into ops (`load`, `stop`, `pos`, `seek`, `pause`, `speed`, …), one frame per
  command, sent down `/sessions/events` to the device that asked for
  `?speech=frames`. The app applies them to its player.
- **Reads are answered here**, from `Model`: the phone's last report
  (`POST /speech/state`) with every op since applied to it, and `time-pos`
  run on by the clock while playing. Nothing crosses the link to answer a
  read.
- **Old news moves only the playhead.** A report whose `seq` is behind the
  last frame sent does not undo a jump just made.

The same mechanism carries the music, hand-off and book players
(`CHANNELS`).

## The relay (fallback, and older apps)

With no frames device connected, a connection is passed through to
`MEDIA_SPEECH_FRAMES_UPSTREAM`, normally `media-ipc-relay`
(`packages/core/src/agent_media_core/entrypoints/ipc_relay.py`; unit
`deploy/systemd/agent-media-speech-relay.service`):

```
media-ipc-relay --listen 127.0.0.1:16614 --upstream <phone>:6614 --spares 3 --max-age 45
```

Every `media` process is short-lived, so each reply used to open its own TCP
connections to the phone: one round trip before a byte (430 ms on a
long-distance link, 27 Sep 2026) and a second more whenever the SYN was lost.
The relay keeps a few connections to the player open in advance ("spares",
default 2), joins each local client to one, and recycles spares after
`--max-age` seconds so a connection a dozing phone or a NAT dropped is
never handed out. Through it connects cost ~0 ms, which would fool the
breaker, so the relay publishes the far side's connect time in
`$XDG_STATE_HOME/agent-media/relay/<port>.json` for `sinks/_mpv_ipc.py` to
size its budget from. Replies also claim and play in one message
(`am-claim-play`), one round trip instead of two.

If speech goes silent with an older app, check the relay first:

```
systemctl --user status agent-media-speech-relay
```

Long-running services started before an endpoint change keep their old
endpoint until restarted.

## Do not poll the phone

A read of the phone's player costs 0.6-2.4 s over a mobile link. In one
measurement 20 reads in a row gave 11 answers, then "skipped, endpoint slow"
for the rest: the slow-endpoint breaker is shared on disk, so a test loop
that polls blinds real replies too. Follow on the row's clock instead (see
[follow-along.md](follow-along.md), "The app's clock"). After a test that
tripped it, `_mpv_ipc.reset_breaker()` forgets the state.

## "Speaking, but nothing is playing"

The model says playing, the phone is silent. Read
`$XDG_STATE_HOME/agent-media/frames-timing.log` (the last 300 lines, one
JSON object each, `ch` naming the channel) before blaming the phone:

| event | means |
|---|---|
| `sent` | a frame was queued for the stream, with its seq and ops |
| `delivered` | the stream wrote it to the socket (no more than that) |
| `played` | the phone says it started a clip (playlist index, clip id) |
| `reported` | the phone's first report on that seq |
| `duration` | the phone first knew the clip's length |
| `lost` | a report still behind a frame older than `LOST_S` |

The failure this was built for (17 Oct 2026): a chime's frames were
`delivered` but never `played` or `reported`. Every later report carried an
older seq, and old news only moves the playhead, so `idle` never got
through; with no duration to clamp it, the playhead ran on for hours over a
phone that kept reporting idle. Now a report behind a frame unacknowledged
for `LOST_S` (`MEDIA_SPEECH_FRAMES_LOST_S`, default 15 s) is taken as the
truth and traced as `lost`.

Two ways the phone loses frames: one arriving before its player is up is
dropped (the app's side of the fix holds them until it is), or the stream died unnoticed.

For a queue that will not start at all, see
[speech-queue.md](speech-queue.md).
