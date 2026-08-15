# agent-media

The agent's audio/music stack — gives coding agents a voice, whole-house
playback control, and music awareness. Runs on any Linux box (x86 or ARM)
or Termux on Android.

```
packages/
├── core/             intake → route → render → sink   the spine (ships the edge engine only)
├── engine-openai/    ┐
├── engine-qwen/      ├─ optional render engines (TTS), discovered via entry points
├── engine-realtime/  ┘
├── intake-matrix/    ┐
├── intake-ha/        ├─ optional intake adapters (event sources)
├── intake-codex/     ┘
├── snapcast-room/    am-snap: whole-house Snapcast routing CLI
├── visual/           generated-image canvas + touch audio controller alongside TTS
└── voice-bridge/     HA Assist transcripts → tmux panes (or a waiting `converse`)
examples/
└── agent-media-engine-espeak/   reference render-engine plugin to copy
```

Core is small and self-contained; optional capabilities (extra TTS engines,
extra event sources) are separate packages that core discovers at runtime — it
never imports them. A base install is zero-config: the `edge` engine needs no
API key. See **[`docs/reference/extensions.md`](./docs/reference/extensions.md)** for the contract.

> **Music recommendation** lives in its own repo now:
> [`davidj4tech/astrotunes`](https://github.com/davidj4tech/astrotunes) — picks
> tracks from your transit chart, time of day, mood, and weather, and queues
> them through core's music sink over the media-mcp boundary.

## Installing

```sh
pip install "agent-media-core @ git+https://github.com/davidj4tech/agent-media#subdirectory=packages/core"
media-setup init              # writes ~/.config/agent-media/config.toml
media-setup install-services  # installs only the services this host's roles want
media-setup install-hooks     # wire the agent side (Claude Code Stop/Notification)
```

`init` guesses the roles and says so; edit the file if the guess is wrong.

### One machine or several

A host declares what it **can do**, and services are selected from that rather
than from its name:

| role | means |
|---|---|
| `observe` | there is a mic or a dialer here worth watching |
| `render` | there are audio sinks here that can be silenced |
| `origin` | this machine produces the text to be spoken |

```toml
[host]
roles = ["observe", "render", "origin"]   # one machine, standing alone
```

```toml
[host]
roles = ["observe", "render"]             # a phone in a larger setup

[peers.hub]
host  = "the-other-machine"
roles = ["render", "origin"]
```

**That is the entire difference between standalone and federated** — a peers
table with entries in it, or without. There is no mode to switch, and nothing
else in the configuration changes.

Roles are also what keeps two supervisors off one audio socket: `call-guard`
requires `observe`, while `call-hold-consumer` (the same binary with detection
switched off, for a machine that renders but cannot observe) requires `render`
and *conflicts* with `observe`. Installing both on one host is what broke
barge-in here for a fortnight.

Hostnames belong in `config.toml` and nowhere else — code asks for the machine
that can do a thing, never for a machine by name.

## Packages

### [`core/`](./packages/core/) — `agent-media-core`

The spine. Subdirs mirror the data flow:

- **`intake/`** — event sources baked into core: Claude Code Stop/Notification hooks, the pi hooks, and the CLI (`media say`). Other sources are separate packages (see below).
- **`route/`** — coordinator: content-type-aware Mopidy ducking/pause, local MPRIS browser pause, remote MPRIS via SSH
- **`render/`** — the `edge` TTS engine (zero-config default) plus a registry that dispatches any other engine to an installed plugin, with automatic fallback to edge
- **`sinks/`** — speech (long-running mpv broker over IPC), music (Mopidy/MPD), and book (audiobook/podcast) channels
- **`state/`** — SQLite: now-playing, history, errors

Also exposes an MCP server (`media-mcp`) for tool-based control from Claude.

See **[`packages/core/README.md`](./packages/core/README.md)** for the full
configuration reference (env vars, services, MPRIS, Snapcast).

### Render engines (TTS plugins)

Optional engines, each registered under the `agent_media.render_engines`
entry-point group and selected with `MEDIA_RENDER_ENGINE=<name>`:

| package | engine | notes |
|---|---|---|
| [`engine-openai/`](./packages/engine-openai/) | `openai` | OpenAI TTS; shells out to a Python with the `openai` lib |
| [`engine-qwen/`](./packages/engine-qwen/) | `qwen` | Qwen / DashScope; stdlib-only |
| [`engine-realtime/`](./packages/engine-realtime/) | `realtime` | OpenAI Realtime over WebSocket |

[`examples/agent-media-engine-espeak/`](./examples/agent-media-engine-espeak/)
is a complete reference engine to copy when writing your own.

### Intake adapters (event sources)

Optional sources, each its own package depending on `agent-media-core` and
shipping a console-script daemon/hook:

| package | command | source |
|---|---|---|
| [`intake-matrix/`](./packages/intake-matrix/) | `media-intake-matrix` | a Matrix room → speech/music/book |
| [`intake-ha/`](./packages/intake-ha/) | `media-intake-ha-sse` | Home Assistant SSE event stream |
| [`intake-codex/`](./packages/intake-codex/) | `media-hook-codex` | Codex (OpenAI CLI) turn output |

### [`snapcast-room/`](./packages/snapcast-room/) — `snapcast-room`

The snapcast/pipewire plumbing: `am-snap`, a terse CLI over Snapcast's JSON-RPC
for whole-house routing (join a room to a channel, set volume, mute) across
multiple snapservers. (`aar-snap` is kept as an alias.)

### [`visual/`](./packages/visual/) — `agent-media-visual`

A picture for every spoken reply: a full-bleed SSE web canvas
(`media-visual-canvas`, systemd unit included) that any phone/TV browser
leaves open, plus `media-visual`, which shapes a reply into an image prompt
(evolving one continuous scene per session), generates it (pluggable
`agent_media.visual_engines`, built-in Venice), and pushes it to one or more
canvases. Tap the canvas for a touch audio controller mirroring the tmux
popup. Opt-in Stop-hook wiring: `MEDIA_SPEECH_VISUAL=1`.

### [`voice-bridge/`](./packages/voice-bridge/) — `tmux-voice-bridge`

Transcript companion. It does **not** capture or transcribe audio — HA Assist
on a phone/earbud does the mic, endpointing and STT, then POSTs the text to
this shim's OpenAI-compatible `/v1/chat/completions`. The shim parses a small
command grammar and injects the rest as keystrokes into a target tmux pane
(local or over SSH).

One exception to the keystroke path: if core's MCP `converse` tool is waiting
on the rendezvous socket, the transcript goes there instead — see
[`capture/rendezvous.py`](./packages/core/src/agent_media_core/capture/rendezvous.py).

Install it like any other package here (`pip install -e packages/voice-bridge`).
This directory is the canonical copy; the standalone `davidj4tech/tmux-voice-bridge`
repo is retired. It stays a *peer* of core rather than an intake adapter — it
injects keystrokes into tmux, it doesn't feed the speech pipeline — and it
installs standalone with no agent-media present, which is why the rendezvous
client is duplicated rather than imported. See docs/reference/restructure.md Phase 5.

---

## Quick start

```sh
# 1. Clone and create a venv
git clone https://github.com/davidj4tech/agent-media
cd agent-media
python3 -m venv .venv && source .venv/bin/activate

# 2. Install core (editable — changes take effect immediately)
pip install -e packages/core

# 3. (optional) Add engines / intake sources you want
pip install -e packages/engine-openai      # MEDIA_RENDER_ENGINE=openai
pip install -e packages/intake-matrix       # media-intake-matrix daemon

# 4. Configure — create ~/.config/agent-media.env
#    See packages/core/README.md for all options. Minimal example:
cat > ~/.config/agent-media.env << 'EOF'
MEDIA_RENDER_ENGINE=edge
MEDIA_RENDER_VOICE_EDGE=en-GB-SoniaNeural
EOF

# 5. Wire services and Claude Code hooks
media-setup

# 6. Source the tmux control surface (add to tmux.conf.local)
# source-file ~/.local/share/agent-media/media.tmux
```

A base install (`edge` engine, core hooks) needs no API keys.

## Multi-host setup (mel → sp4r example)

mel is headless; sp4r is the laptop with speakers and a browser.

- mel renders TTS and routes audio to sp4r's Snapcast (`MEDIA_SPEECH_DEFAULT_TARGET=rooms`)
- sp4r runs `snapserver` + `snapclient` + Mopidy feeding `am-music`
- When mel speaks, sp4r's Chrome/browser pauses automatically via SSH MPRIS:
  set `MEDIA_MPRIS_SSH_HOSTS=sp4r` in mel's env file

See `packages/core/README.md` → *Remote MPRIS* for details.

## History

Assembled in May 2026 from previously separate repos, then slimmed into a small
core + optional packages:

- `davidj4tech/mpv-mcp` → `media-mcp` (Node) → retired in favor of `core.mcp_server` (Python)
- `davidj4tech/agent-audio-relay` → shrunk + renamed to `packages/snapcast-room/` (the rest absorbed into `core/`)
- `davidj4tech/tmux-voice-bridge` → `packages/voice-bridge/`
- render engines (openai/qwen/realtime) and intake adapters (matrix/ha/codex) lifted out of core into their own packages behind the extension contract
- `astrotunes` extracted to [its own repo](https://github.com/davidj4tech/astrotunes)
