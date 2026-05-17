# Snapcast pipeline unification review

Status: draft, 2026-05-17. Decisions land in RESTRUCTURE.md or in
individual issues once we agree.

## Today's reality

### p8ar (Termux/Android, the phone)
```
intake/* ──► route/coordinator ──► sinks/speech (mpv, openal)  ──► p8ar speaker (local only)
                              └──► sinks/music  (MPD → Mopidy) ──► p8ar speaker (local only)
                              
mcp_server (stdio + HTTP :8765) sits on top of all of that.

Snapclient services on p8ar:
  snapclient-mel  → tcp://mel:1704   (receive from mel's snapserver)
  snapclient-sp4r → tcp://sp4:1704   (receive from sp4r's snapserver)

Legacy still running:  mpv, mpv-music, mpv-voice, sam-listener,
                       agent-audio-relay (watch-dir)
```

### sp4r (Linux desktop, the room hub)
```
pipewire null-sinks:  aar, aar-music     (defined in 30-aar-sinks.conf)

snapfifo-aar.service:        parec aar.monitor       → /tmp/snapfifo-aar
snapfifo-aar-music.service:  parec aar-music.monitor → /tmp/snapfifo-aar-music
aar-sink-stream.service:     null-sink → ffmpeg → HTTP MP3 fan-out (parallel path)

snapserver:  reads both fifos → tcp://0:1704 → fans out to all snapclients

Also: aar-clip-server, aar-mpv-tunnel, agent-audio-relay-forwarder,
opencode-tts-watcher (currently no-op — RELAY_FWD_REMOTE commented out)
```

### Rooms (mel, p8ar, others)
Snapclients connect to sp4r:1704, receive aar and aar-music streams,
play to local audio output.

## The mismatch

- **Speech and music *on* p8ar play through p8ar's speaker only.**
  The whole-house snapcast pipeline is sp4r-rooted. Nothing routes
  p8ar's rendered audio into sp4r's `aar` or `aar-music` pulse sinks.
- **Cross-host TTS** used to work via `agent-audio-relay-forwarder` →
  watch-dir on p8ar → playback. That path is currently idle on the
  forwarder side (env commented out).
- **Two parallel fan-outs on sp4r**: snapcast (TCP, opus, low-latency,
  multi-room sync) and `aar-sink-stream` (HTTP MP3, simpler clients).
  Both alive. Unclear which one is the "right" path going forward.
- **Coordinator scope**: `route/coordinator.py` ducks Mopidy on
  p8ar. It can't duck sp4r's `aar` pulse sink, and it can't pause
  whatever is producing `aar-music` from another host (the deferred
  movie/video case).

## Design choices to make

### 1. Where does Claude's voice play?

Three coherent answers (pick one):

**A. Phone-only (today's state).** p8ar speaks through its own
speaker. Snapcast is a separate world. Simple. Loses the "Claude
spoke in the kitchen while I'm cooking" use case.

**B. Always whole-house.** p8ar renders TTS and ships the audio to
sp4r's `aar` pulse sink (via PulseAudio-over-network, ssh-tunneled
PA socket, or a `core.sinks.pulse` adapter pointed at sp4r's
remote sink). Snapcast carries it to all rooms. Loses local-only
notifications.

**C. Target-aware.** Event has a `target` (already in the type
system — currently always `"local"`). `target="local"` → p8ar
speaker. `target="rooms"` → sp4r's `aar`. `target="kitchen"` →
specific snapclient. Coordinator + sink adapters know which
target to use. Most flexible; most work.

### 2. Who owns the `aar-music` sink on sp4r?

Currently nothing on sp4r drives the music sink — Mopidy is on
p8ar. To get music into snapcast, options:

**A.** Move Mopidy to sp4r (drop p8ar's Mopidy). Mopidy on sp4r
plays to `aar-music` pulse sink → snapfifo → snapcast. p8ar
becomes a snapclient. Simpler conceptually; requires moving the
state (library, playlists, listenbrainz token).

**B.** Keep Mopidy on p8ar, route its audio output to sp4r's
`aar-music` sink via PulseAudio-over-network. Status quo on the
library side; new network audio path.

**C.** Run Mopidy on both, treat sp4r as the "house" instance
and p8ar as the "phone" instance. State diverges. Probably worst.

### 3. Retire `aar-sink-stream` (HTTP MP3 fan-out)?

Two questions: is anything actually consuming the HTTP MP3 feed
right now (browser tab somewhere?), and is snapcast enough for
every client we care about? If both are "no problem to drop" →
retire it. If a browser/web-app uses it → keep, document.

### 4. The movie/video case (deferred 2026-05-17)

Movies on sp4r play via mpv → sp4r's default sink. When agent
speech wants to interrupt, ducking dialogue is wrong. We want
pause-resume on the upstream player. Options:

**A.** Per-player IPC: coordinator knows how to pause mpv via its
JSON socket. Bound to mpv only.

**B.** Generic "pause this stream" via pipewire/pactl on the
producer side (sp4r). Requires identifying the right stream by
client name.

**C.** Treat all sp4r `aar`-bound audio as duckable; treat sp4r's
*default* pulse sink (where movies go) as pause-only by some
naming convention.

Probably **A** wins for now (mpv is what we use); **B**/**C** are
clever but flimsy.

### 5. Retire `audio-relay/` partly or fully?

What `audio-relay/` provides today, broken down by component:

| Component | Status today | Where it could go |
|---|---|---|
| snapfifo-aar*.service (parec → fifo) | sp4r-active, snapcast-critical | Stays; or moves to `core/services/snapcast/` if we want the whole pipeline under core |
| aar-sink-stream (HTTP MP3 fan-out) | sp4r-active, consumers unclear | Decision #3 above |
| aar-clip-server (HTTP file server) | sp4r-active | Useful for clip-archive browsing; stays unless we kill the archive |
| aar-mpv-tunnel (ssh socket fwd) | sp4r-active, but mpv-tts retired on p8ar | Probably retire — was tunneling to a sink we just deleted |
| agent-audio-relay-forwarder | sp4r-active but no-op | Retire — superseded by Decision #1 (proper target-aware routing) |
| opencode-tts-watcher | sp4r-active | Migrate to `media-hook-opencode` (core/intake) |
| `tts.tmux` plugin + `tts-tmux-install` | p8ar-active, in tmux.conf.local | Adapter: rewrite to use mpc + media-mcp; drop dependency on audio-relay's bins |
| Python shell bins (`tts-popup` etc.) | scattered | Decide per-bin; many are duplicated by media-mcp tools |

A pragmatic end state: `packages/audio-relay/` shrinks to just the
snapcast/pipewire plumbing (FIFOs, sink-stream HTTP, clip-server)
that's distinctly *sp4r infrastructure*, and rename it
`packages/snapcast-room/` or similar. Everything else either dies
or moves into `core/`.

## Suggested order of operations

If we agree on the broad shape (let's say **1C + 2B + 3=defer
until consumer audit + 4A + 5 partial**), the work breaks into
five PRs that can land independently:

1. **`core.sinks.pulse` adapter** — talks to a remote PA socket
   (sp4r's `aar` / `aar-music`). Wires up `target="rooms"` in the
   type system. Smallest concrete PR.

2. **`mcp__media-mcp__say` target plumbing** — accepts
   `target="rooms"` etc., picks the right sink. No coordinator
   change yet.

3. **Coordinator-aware sp4r pulse ducking** — when speech goes to
   `aar` (rooms), duck `aar-music` (via `pactl set-sink-volume`),
   not Mopidy's MPD. Bigger coordinator refactor (target-aware
   policy).

4. **Movie/video sink (sp4r mpv pause-resume)** — `core.sinks.mpv`
   that talks to a discovered mpv IPC socket on sp4r. Coordinator
   policy adds "PAUSE_PRODUCER" strategy for video content type.

5. **audio-relay retirement** — retire `aar-mpv-tunnel`,
   `agent-audio-relay-forwarder`, `opencode-tts-watcher` (after
   `media-hook-opencode` lands); rename remaining bits.

## Open questions for David

- Does **whole-house TTS** matter to you, or is phone-only fine?
  (Choice #1)
- Where do you want Mopidy to live long-term — p8ar (phone) or
  sp4r (room hub)? (Choice #2)
- Anything consuming `aar-sink-stream`'s HTTP MP3 right now (web
  app, browser, embedded device)? (Choice #3)
- Movies — only mpv, or do you also play in browser / VLC? (#4)
- Is there a snapclient set in the kitchen / bedroom / etc. that's
  the actual use case, or are we mostly designing for sp4r itself
  + p8ar + mel?
