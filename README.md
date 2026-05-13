# agent-media

The agent's audio/music stack — a small family of tools that lives on a
phone (or any Termux/Linux box) and gives coding agents a voice,
playback control, and (soon) a sense of musical taste.

```
┌────────────────────────────────────┐
│ packages/                          │
│ ├── media-mcp/   playback control  │  Node, MCP server + HTTP/UI
│ ├── audio-relay/ TTS clip delivery │  Python, agent voice clips
│ └── astrotunes/  what to play      │  Python, transit-aware picks
└────────────────────────────────────┘
            │
            ▼
   one phone running mpv channels
   (music + tts, with auto-ducking)
```

## Packages

### [`media-mcp`](./packages/media-mcp/)

Multi-channel `mpv` control surface — MCP server, HTTP/JSON API, and a
mobile-first PWA. Runs three mpv daemons (`music`, `tts`, `voice`) with
property-event ducking. Designed to live on a phone on Tailscale.

> Renamed from `mpv-mcp`. Kept the broader name to leave room for video
> channels and additional media backends.

### [`audio-relay`](./packages/audio-relay/)

The Python relay that captures agent TTS output (Claude Code Stop hook,
Codex stdin, OpenCode session poller) and delivers clips to a playback
target — typically `media-mcp`'s `tts` channel. Distributed on PyPI as
`agent-audio-relay`.

### `astrotunes` *(in progress)*

Music recommendation tool. Given current planetary transits, time of
day, mood, activity, and Melbourne weather, picks tracks and routes
them to either `media-mcp` (local phone) or Mopidy (remote). Uses
`kerykeion` for transit computation.

## Install

Each package has its own install path:

```sh
# media-mcp (Node, runit services on Termux)
cd packages/media-mcp && ./install.sh

# audio-relay (Python, pip-installable)
pip install --user ./packages/audio-relay
# or from PyPI:
pip install --user agent-audio-relay

# astrotunes (Python, in progress)
pip install --user ./packages/astrotunes
```

A top-level installer that does all three may follow once `astrotunes`
is past prototype.

## History

`agent-media` was assembled in May 2026 from two previously separate
repos:

- `davidj4tech/mpv-mcp` (renamed → `media-mcp` → restructured into `packages/media-mcp/`)
- `davidj4tech/agent-audio-relay` (subtree-imported with full history → `packages/audio-relay/`; old repo archived)

`astrotunes` is new.
