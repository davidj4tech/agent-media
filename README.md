# agent-media

The agent's audio/music stack — gives coding agents a voice, whole-house
playback control, and music awareness. Runs on any Linux box (x86 or ARM)
or Termux on Android.

```
┌──────────────────────────────────────────────────────┐
│ packages/                                            │
│ ├── core/          intake → route → render → sink    │  Python — the spine
│ ├── snapcast-room/ am-snap: snapcast routing CLI      │  Python — plumbing
│ ├── voice-bridge/  STT (mic → text → intake)          │  Python — sibling
│ └── astrotunes/    what to play                       │  Python — recommender
└──────────────────────────────────────────────────────┘
```

## Packages

### [`core/`](./packages/core/) — agent-media-core

The spine. Five subdirs that mirror the data flow:

- **`intake/`** — event sources: Claude Code Stop/Notification hooks, Codex hook, Home Assistant SSE, Matrix, CLI (`media say`)
- **`route/`** — coordinator: content-type-aware Mopidy ducking/pause, local MPRIS browser pause, remote MPRIS via SSH
- **`render/`** — TTS engines (edge / openai / qwen / realtime) with automatic fallback
- **`sinks/`** — speech (long-running mpv broker over IPC) and music (Mopidy/MPD)
- **`state/`** — SQLite: now-playing, history, errors

Also exposes an MCP server (`media-mcp`) for tool-based control from Claude.

See **[`packages/core/README.md`](./packages/core/README.md)** for full
configuration reference (env vars, services, MPRIS, Snapcast).

### [`snapcast-room/`](./packages/snapcast-room/)

The snapcast/pipewire plumbing that survived the restructure: `am-snap`, a
terse CLI over Snapcast's JSON-RPC for whole-house routing (join a room to a
channel, set volume, mute) across multiple snapservers. The former
`agent-audio-relay`'s render engines, agent hooks, watcher and clip-server all
moved into `core/`; this is what remained. (`aar-snap` is kept as an alias.)

### [`voice-bridge/`](./packages/voice-bridge/)

STT companion — mic capture → transcribe → `submit_event` into core's intake
pipeline.

### [`astrotunes/`](./packages/astrotunes/) *(in progress)*

Given current planetary transits, time of day, mood, activity, and local
weather, picks tracks and queues them via core's music sink.

---

## Quick start

```sh
# 1. Clone and create a venv
git clone https://github.com/davidj4tech/agent-media
cd agent-media
python3 -m venv .venv && source .venv/bin/activate

# 2. Install core (editable — changes take effect immediately)
pip install -e packages/core

# 3. Configure — create ~/.config/agent-audio-relay.env
#    See packages/core/README.md for all options. Minimal example:
cat > ~/.config/agent-audio-relay.env << 'EOF'
MEDIA_RENDER_ENGINE=edge
MEDIA_RENDER_VOICE=en-GB-SoniaNeural
EOF

# 4. Wire services and Claude Code hooks
media-setup

# 5. Source the tmux control surface (add to tmux.conf.local)
# source-file ~/.local/share/agent-media/media.tmux
```

## Multi-host setup (mel → sp4r example)

mel is headless; sp4r is the laptop with speakers and a browser.

- mel renders TTS and routes audio to sp4r's Snapcast (`MEDIA_SPEECH_DEFAULT_TARGET=rooms`)
- sp4r runs `snapserver` + `snapclient` + Mopidy feeding `am-music`
- When mel speaks, sp4r's Chrome/browser pauses automatically via SSH MPRIS:
  set `MEDIA_MPRIS_SSH_HOSTS=sp4r` in mel's env file

See `packages/core/README.md` → *Remote MPRIS* for details.

## History

Assembled in May 2026 from previously separate repos:

- `davidj4tech/mpv-mcp` → `media-mcp` (Node) → retired in favor of `core.mcp_server` (Python)
- `davidj4tech/agent-audio-relay` → `packages/audio-relay/` → shrunk + renamed to `packages/snapcast-room/` (the rest absorbed into `core/`)
- `davidj4tech/tmux-voice-bridge` → `packages/voice-bridge/`
- `astrotunes` new
