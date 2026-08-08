# Music: local playout + Snapcast routing

Status: draft, pre-execution. Owner: David.

## Why

Today the music channel has exactly one path: MPD → Mopidy on mel → the `am-music`
PipeWire sink → a Snapcast `am-music` stream. That path has three coupled problems:

1. **403 on datacenter IPs.** mel (IONOS) and red5 (Hetzner) get `GStreamer error:
   Forbidden` on most YouTube CDN URLs. The rooms path papers over it by forcing a
   `tv`/`android_vr` yt-dlp client; that breaks every time YouTube changes formats.
2. **The residential workaround is off-channel.** `play-music --phone` shells to
   `ssh p8ar bin/play-local`, which downloads on the phone's residential IP and loads
   the file into a phone-local `mpv-music` over the **legacy `agent-audio-relay`
   socket**. agent-media has no idea that player exists.
3. **Because it's off-channel, it doesn't duck and it isn't whole-house.** The
   coordinator ducks `self.music` (Mopidy) on speech; the phone mpv isn't `self.music`,
   so TTS talks over it. And the phone mpv is a private sink, not a Snapcast stream, so
   "play locally" means "play *only* on the phone."

All three are the same root cause: **local playout is not a member of the music
channel.** `play-local` is a hand-rolled, off-channel reimplementation of something the
`book` channel already does on-channel (download on phone via `audiobook-fetch`, play a
local file through mpv-over-IPC, ducked/paused by the coordinator — see
`sinks/book.py` + `library.py:start_fetch`).

## The reframe (David): it's mostly a Snapcast-orchestration problem

Grounding against the live setup (2026-06-30):

- **The snapservers are already distributed.** mel *and* red5 each run a snapserver.
  red5 is already the live rooms hub — `hpo, sp4r, p8ar, tvr` are connected to red5's
  `am`/`am-music` groups; mel's clients are all `/off`. The rooms-audio-hub migration
  has effectively happened.
- **agent-media was designed for Snapcast targets but never wired them.** `types.py:58`
  anticipates `Target` names `local, snapcast-mel, snapcast-sp4r, …`, but there is **no
  Snapcast control code** anywhere (no `Group.SetStream`, no `Client.SetVolume`). The
  music sink hard-rejects any target but `local` (`sinks/music.py:165`). Snapcast
  control was deferred.

So the unifying primitive:

> The music channel's **target** = *which snapserver stream a group of rooms listens
> to*. The 403 is purely about *which host runs the player that feeds that stream's
> source*. Decouple "where control/state/duck live" from "where the bytes are fetched
> and emitted," and every symptom above resolves.

With that decoupling:

- **Duck** = Snapcast group/client `SetVolume` (or the source player's own volume) —
  independent of who is sourcing.
- **Whole-house from a residential fetch** = feed red5's `am-music` *stream source* from
  a player on a residential IP (the phone) rather than a datacenter player. Snapserver
  sources can be `pipe://`, `tcp://`, `process://`, so a phone→red5 source is native.
- **Phone-only / offline** = the phone plays its local mpv directly (today's
  `play-local`), now as a duckable channel member (duck = mpv volume over IPC).

### Two variants for "residential source, whole-house sync"

- **A. One active server (red5), multiple stream sources.** red5's snapserver defines
  an extra `am-music-residential` source fed over the network by the phone's player.
  `Group.SetStream` switches the rooms between `am-music` (mel Mopidy) and the
  residential source. Clients never move. **Recommended** — Snapcast-native, no client
  churn. Cost: phone uplinks PCM/opus to red5 (loses play-local's ~1/5-bytes win, but
  only when whole-house is actually wanted).
- **B. Truly distributed servers.** The phone runs its own snapserver fed by its local
  mpv; nearby clients point at it. More literally "distributed snapservers," but a
  snapclient binds **one** server at a time, so changing the active source means
  repointing/restarting clients. Keep as a fallback for phone-cluster-only scenes.

For the **phone-is-the-only-listener** case, neither is needed — local mpv is already
optimal; it just needs to be duckable + controllable (see Stage 1).

## Plan

### Stage 1 — make phone-local playout a real, duckable music backend (do now)

The immediate win David asked for. No Snapcast required.

- Add a music playout backend that drives the phone's mpv over IPC, implementing the
  `Sink` protocol (`types.py:78`): `play/pause/resume/stop/duck/unduck/position/
  now_playing_uri`. `duck` = mpv `set volume`; `position`/`now_playing_uri` = mpv IPC
  reads. Model on `sinks/book.py` + `_mpv_ipc.py`.
- Fold `play-local`'s download into the channel by reusing the book channel's
  phone-fetch machinery (`library.start_fetch`, cache-by-id) instead of a separate
  script. `play-local` becomes the music channel's `local` playout path.
- Migrate the phone mpv off `agent-audio-relay/mpv-music.sock` onto agent-media's
  socket convention (`~/.local/state/agent-media/sink-music.sock`).
- Result: `media music play --local <url>` downloads on the phone, plays on the phone,
  and **ducks under speech** because it's now `self.music` for that target.

### Stage 2 — Snapcast control in agent-media (the spine of the reframe)

- New Snapcast control module: JSON-RPC client (`Server.GetStatus`, `Group.SetStream`,
  `Client.SetVolume`/`Group.SetVolume`). Config: `MEDIA_SNAP_JSONRPC_HOST/PORT`
  (default red5).
- Implement `Target` resolution for `music`: `snapcast-<host>` / `rooms` → select the
  group's stream + duck via Snapcast volume; fill the `NotImplementedError` at
  `sinks/music.py:165`.
- Move `play-music`'s snapclient-counting auto-route (rooms vs phone) into the channel
  as Target selection; retire the bash wrapper.

### Stage 3 — residential source onto the rooms server (Variant A)

- Define an `am-music-residential` stream source on red5's snapserver fed by the phone
  (`tcp://` or `process://`).
- Teach the music channel: target `rooms` + content that 403s on datacenter → fetch on
  phone, emit to the residential source, `Group.SetStream` the rooms onto it.

### Stage 4 — control plane on red5

- Run agent-media's music channel control on red5 so "controlled through red5's music
  channel" is literal: red5 owns state, queue, duck signal, and Snapcast orchestration;
  players (mel Mopidy, phone mpv, residential source) are interchangeable emitters.
  Gated on the red5 rooms-audio-hub buildout.

## Open questions

- Stage 3 bandwidth: is uplinking PCM/opus phone→red5 acceptable on mobile data, or
  gate the residential-source path to home wifi only?
- Duck mechanism for Snapcast: per-group `SetVolume` vs ducking the source player's
  volume — which gives cleaner restore and avoids fighting the existing Mopidy duck?
- Do we keep mel's snapserver at all, or collapse fully onto red5 once Stage 4 lands?
