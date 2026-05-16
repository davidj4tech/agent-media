# agent-media-core

Core library for agent-media. Event-driven pipeline:

```
intake/  ─►  route/  ─►  render/  ─►  sinks/
                  ▲                       │
                  └─────  state/  ◄───────┘
```

See `../../RESTRUCTURE.md` for the full architecture and migration plan.
This package is in **Phase 0 scaffold** state — no behaviour yet, just
type contracts.

## Modules

- `intake/` — event sources (hooks, CLI, MCP, HA, Matrix, legacy watcher).
- `route/` — policy: pre-emption, per-source voice, content-type-aware
  interruption (duck vs pause-and-resume), target selection.
- `render/` — text → audio. Engines: edge, openai, qwen, realtime.
- `transcribe/` — audio → text. HA passthrough today; Whisper later.
- `capture/` — mic capture.
- `sinks/` — `sink-speech` (mpv/openal), `sink-music` (Mopidy).
- `state/` — SQLite-backed queue, now-playing, history, errors,
  pause-resume positions.
- `entrypoints/` — hook scripts, CLI, MCP server.
