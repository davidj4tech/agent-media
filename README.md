# agent-media

The agent's audio/music stack — a small family of packages that gives
coding agents a voice, playback control, and (soon) a sense of musical
taste. Lives on a phone (Termux) or any Linux box.

```
┌──────────────────────────────────────────────────┐
│ packages/                                        │
│ ├── core/         intake → route → render → sink │  Python — the spine
│ ├── audio-relay/  whole-house snapcast pipeline  │  Python — sp4r/rooms
│ ├── voice-bridge/ STT (mic → text → intake)      │  Python — sibling
│ └── astrotunes/   what to play                   │  Python — recommender
└──────────────────────────────────────────────────┘
```

## Packages

### [`core/`](./packages/core/) — agent-media-core

The spine. One package, five subdirs that mirror the data flow:

- `intake/` — event sources (Claude Code hook, Codex hook, HA-SSE, Matrix)
- `route/` — policy + coordinator (content-type aware ducking / pause-resume)
- `render/` — TTS engines (edge / openai / qwen / realtime) with fallback
- `sinks/` — speech (mpv) and music (Mopidy/MPD)
- `state/` — SQLite history, errors, now-playing

Plus an `mcp_server.py` exposing the control surface over MCP — stdio
for local Claude Code (`claude mcp add media-mcp -s user -- media-mcp`)
and streamable-HTTP for remote callers (`media-mcp-http` on
`MEDIA_MCP_HOST:MEDIA_MCP_PORT`, default `127.0.0.1:8765`).

### [`audio-relay/`](./packages/audio-relay/)

The whole-house snapcast pipeline. Pumps p8ar's pipewire null-sinks
(`aar`, `aar-music`) into snapcast FIFOs so every room hears agent
voice + music. Also hosts the clip-server, mpv-tunnel, and the
forwarder/watcher pair that fan TTS clips to remote hosts.

Distributed on PyPI as `agent-audio-relay`.

### [`voice-bridge/`](./packages/voice-bridge/)

STT companion — mic capture → transcribe → submit into core's intake
pipeline. Sibling to core, not nested under it.

### [`astrotunes/`](./packages/astrotunes/) *(in progress)*

Given current planetary transits, time of day, mood, activity, and
Melbourne weather, picks tracks and queues them via core's music sink.

## Install

```sh
# core (Python, editable) — provides media-mcp / media-hook-* / media-setup
pip install --user -e packages/core

# audio-relay (Python, pip-installable)
pip install --user packages/audio-relay

# astrotunes (in progress)
pip install --user packages/astrotunes
```

`media-setup` (from core) installs Claude Code hooks, services (runit on
Termux / host-runit, systemd `--user` on regular Linux — auto-detected,
override with `--backend`), and migrates legacy `CLAUDE_TTS_*` / `AAR_*`
env keys to `MEDIA_*`.

## History

Assembled in May 2026 from previously separate repos:

- `davidj4tech/mpv-mcp` → `media-mcp` (Node) → retired May 2026 in
  favor of `core.mcp_server` (Python, two-transport)
- `davidj4tech/agent-audio-relay` → `packages/audio-relay/`
- `davidj4tech/tmux-voice-bridge` → `packages/voice-bridge/`
- `astrotunes` new
