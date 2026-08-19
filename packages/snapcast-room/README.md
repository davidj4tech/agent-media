# snapcast-room

A terse CLI over [Snapcast](https://github.com/badaix/snapcast)'s JSON-RPC for
whole-house audio routing — "tell room X to listen to channel Y, set its
volume, mute it." It addresses multiple snapservers (e.g. `mel`, `sp4r`)
transparently via a small config that maps logical channel/room names to
`(server, stream-id)` / `(server, client)`.

This package is the snapcast/pipewire plumbing that survived the **agent-media**
restructure. The former `agent-audio-relay` package's TTS render engines, agent
hooks (Claude Code / Codex / OpenCode / HA), the drop-dir watcher, the clip
server and the HTTP fan-out all moved into [`agent-media-core`](../core); what
remained — the snapcast routing CLI — lives here under its own name.

## Install

```sh
uv pip install ./packages/snapcast-room      # or: pip install snapcast-room
```

Pure stdlib, no runtime dependencies.

## CLI

The console script is `am-snap` (the old `aar-snap` name is kept as an alias).

```
am-snap list                  # full state across configured servers
am-snap rooms                 # short room / channel / volume table
am-snap streams               # channels and their backing streams
am-snap join <channel> <room> # make <room> listen to <channel>
am-snap volume <room> <0-100> # set room volume
am-snap mute <room> [on|off]  # set or toggle mute
```

## Config

`~/.config/snapcast-room/am-snap.json` (falls back to the legacy
`~/.config/agent-audio-relay/aar-snap.json` if that's the only one present;
override with `$AM_SNAP_CONFIG`):

```json
{
  "servers": { "mel": "http://mel:1780/jsonrpc", "sp4r": "http://sp4r:1780/jsonrpc" },
  "streams": { "music": { "server": "mel", "stream": "am-music" },
               "voice": { "server": "mel", "stream": "am" } },
  "rooms":   { "study": { "server": "mel", "client": "p8a-music" } }
}
```

`channel` is a key from `streams`; `room` is a key from `rooms` (or a snapcast
client id / name directly if no alias is configured).
