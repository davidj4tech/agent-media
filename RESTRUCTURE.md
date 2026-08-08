# agent-media restructure plan

Status: draft, pre-execution.

## Why

Names and shape have drifted from rapid patching. Currently:
- TTS routing depends on an undocumented length threshold (clip vs stream paths).
- Two mpv brokers exist for speech with separate sockets/AO/failure modes but the same voice.
- Output hooks (Claude Code, Codex, OpenCode, HA-SSE, Matrix) each invent their
  own write-into-drop-dir or bypass-drop-dir contract.
- `sam-listener` runs its own polling + playback (`termux-media-player`) + control
  command surface — a third playback path alongside mpv and Mopidy.
- STT input lives in a separate repo (`tmux-voice-bridge`) but conceptually
  belongs in the same architecture.
- Env vars are `CLAUDE_TTS_*` even though the thing isn't Claude-specific.
- Render engines: `aar-tts-render` accepts edge/openai/qwen but settings.json
  asks for `realtime`, which only the streaming path knows about.

## Target shape

```
agent-media/
└── packages/
    ├── core/
    │   ├── intake/      # event sources: produce { text, source, priority, voice?, target? }
    │   │   ├── hook-claude-code
    │   │   ├── hook-codex
    │   │   ├── hook-opencode
    │   │   ├── ha-sse           # was hooks/ha-tts-bridge.sh (openclaw outbound)
    │   │   ├── ha-stt           # absorbed from tmux-voice-bridge
    │   │   ├── matrix           # was sam-listener
    │   │   ├── watcher          # legacy drop-dir adapter for ext producers
    │   │   ├── cli              # `media say`
    │   │   └── mcp              # MCP server entrypoint
    │   ├── route/       # pre-emption, per-source policy, interruption (duck/pause),
    │   │                # target selection. Content-type aware: music ducks,
    │   │                # audiobooks/podcasts pause-and-resume.
    │   ├── render/      # text → audio: edge / openai / qwen / realtime (first-class)
    │   ├── transcribe/  # audio → text: HA passthrough, future Whisper
    │   ├── capture/     # mic capture (was termux-microphone-record in sam-listener)
    │   ├── sinks/       # sink-speech (mpv, openal), sink-music (Mopidy)
    │   │                # target: local | snapcast-mel | snapcast-sp4r | bt-* | matrix-room
    │   └── state/       # SQLite: queue, now-playing, history, errors
    ├── astrotunes/      # unchanged (returns music recommendations)
    └── voice-bridge/    # permanent sibling package — HA Assist transcripts →
                         # tmux panes (or a waiting core `converse`). NOT an
                         # intake adapter; see Phase 5 for why it doesn't merge.
```

## Invariants (locked)

- **Render engines**: edge, openai, qwen, realtime — all first-class. `realtime`
  is no longer a settings-only string that the drop path rejects.
- **Sockets**: XDG-only (`~/.local/state/agent-media/*.sock`). No `$PREFIX/tmp/`
  symlinks. Legacy paths deleted at Phase 3.
- **Speech AO**: `openal`. Proven to survive BT route changes.
- **TTS paths**: one path. Stream-only render. The clip/stream split was an
  implementation accident around hook timeout — removed.
- **Hook env-file source**: upstreamed. Installed hook no longer needs
  out-of-band patching.
- **Interruption strategy is per-content-type, not global**:
  - `music` → duck (configurable level, default ~15%)
  - `audiobook` / `podcast` / `longform` → pause-and-resume around the
    interrupting speech (with a small lead-in/lead-out window so you don't
    lose the last word). Resume offset is tracked in `state/`.
  - `dj-set` / `mix` → duck (continuous content, pausing breaks the vibe)
  - `ambient` → duck deeper (so speech sits cleanly on top)
  Content type comes from the playing track's metadata (Mopidy URI scheme,
  tags, or explicit override at queue time). `state/` records the current
  type so route/ can pick the right strategy without re-querying.

## Phase plan

Each phase leaves a working system. The order is dictated by "don't break the car."

### Phase 0 — Prep
- Vendor `tmux-voice-bridge` into `packages/voice-bridge/` via git subtree merge
  (preserves history). Repo lookup still works; nothing in production changes yet.
- Add `packages/core/` skeleton with empty modules.
- Define the event schema (`Event`, `Source`, `Priority`, `Target`) and the
  `Sink` interface as types in `packages/core/types.py`.

### Phase 1 — Render consolidation
- Move all four render engines (edge/openai/qwen/realtime) into
  `core/render/`. One signature: `render(text, engine, voice, **opts) -> path`.
- `aar-tts-render` becomes a thin compatibility shim that calls into
  `core.render`. Settings.json `CLAUDE_TTS_ENGINE=realtime` now works end-to-end.
- No external behaviour change yet — just plumbing.

### Phase 2 — Sink consolidation
- Stand up `sink-speech` (single mpv broker, openal, XDG socket).
- Stand up `sink-music` thin wrapper over Mopidy (no behaviour change).
- Retire `mpv-tts` + `mpv-voice` services. Existing watcher dispatches to
  `sink-speech` via new socket path.
- `mpv-music` service stops (Mopidy was already doing the work).
- Legacy drop-dir watcher continues forwarding to sink-speech.

### Phase 3 — Route + state
- Introduce `core/route/` with explicit policy: pre-emption rules, per-source
  voice mapping, per-content-type interruption (duck vs pause-and-resume),
  ducker policy (`aar-mopidy-duck` dissolves into this).
- Content-type detection: map Mopidy URI schemes / tags to type
  (`yt:` → music or longform depending on duration; `podcast:` / `audiobook:`
   → longform; etc.). Allow per-track override at queue time.
- Pause-and-resume implementation: record `(track_uri, position_ms)` in
  state/ before pause; restore on speech end. Lead-in/lead-out window
  configurable (default ~500ms either side so you don't lose words).
- Stand up `core/state/` (SQLite at `~/.local/state/agent-media/state.db`)
  for queue, now-playing (including content-type), history, errors,
  pause-resume positions. Migrations module included.
- All sink ops flow through route → state for observability.
- Delete legacy `$PREFIX/tmp/` socket symlinks. XDG-only from here.

### Phase 4 — Intake migration
Migrate hooks one at a time. Each becomes an intake adapter that posts an
`Event` to route. Order chosen for risk:
1. `hook-claude-code` (your primary path, get it working first)
2. `hook-codex`, `hook-opencode` (similar shape)
3. `ha-sse` (was `hooks/ha-tts-bridge.sh`)
4. `matrix` (was `sam-listener.py`)
   - Move hardcoded `ACCESS_TOKEN` to env / sops.
   - Drop `termux-media-player`; pipe through `sink-speech`.
   - `!pause`/`!skip`/`!replay`/etc. become route-level commands shared with MCP.

### Phase 5 — STT structure (plan B: co-locate, don't merge)
Original plan was to dissolve `packages/voice-bridge/` into core. On closer
read, voice-bridge isn't an intake adapter for the speech pipeline —
it's a peer system that *injects keystrokes* into tmux panes from HA
Assist transcripts. The target tool's own Stop hook then produces the
spoken reply via `core/intake`. Forcing those two responsibilities into
one module tree obscures more than it clarifies.

New plan:
- **Keep `packages/voice-bridge/` as a sibling package** in the monorepo.
  Its `/v1/chat/completions` HTTP shim and tmux paste-buffer injection
  stay where they are.
- `core/transcribe/` stays scaffolded as the slot for *future* STT
  implementations that DO produce text-for-rendering (local Whisper,
  push-to-talk, etc.). Docstring records why it's empty today.
- `core/capture/` likewise — slot reserved for future mic capture
  (push-to-talk, BT button, etc.). Matrix-side mic recording moved
  there if/when the matrix adapter grows a record/send flow.
- voice-bridge can adopt `core._notify` + `core.state` for observability
  without merging.

**Status — done 2026-08-08.** The tree diagram above said "dissolves into
core at Phase 5" for months after this section superseded it; that stale line
is corrected. The substantive gap was that `packages/voice-bridge/` was a May
subtree snapshot while the live code kept developing in the standalone
`tmux-voice-bridge` repo — three commits and 127 lines apart, with the
editable install and systemd unit both pointing outside the monorepo. The
sibling package is now the canonical copy: subtree-pulled, installed from
`packages/voice-bridge`, and the service runs it from agent-media's venv.

What plan B predicted held up. When `converse` needed voice-bridge to hand a
transcript to core, the answer wasn't a merge — it was a unix socket and a
twenty-line stdlib client, precisely because the two are peers with different
lifecycles. `core/capture/` stopped being an empty slot the same day
(`capture/rendezvous.py`).

Still open: voice-bridge adopting `core._notify` / `core.state` for
observability. It logs to the journal and nowhere else.

### Phase 6 — MCP control surface + cleanup
- MCP exposes:
  `speech.pause`, `speech.resume`, `speech.skip`, `speech.repeat_last`,
  `speech.set_target`, `speech.status`, `speech.history [n]`.
- Same primitives available via `media` CLI.
- Delete `packages/audio-relay/` (now a stub or fully empty).
- Run env-var migration script on local settings.json / agent-audio-relay.env.

## Env var migration

| Old                              | New                                    | Notes                              |
|----------------------------------|----------------------------------------|------------------------------------|
| `CLAUDE_TTS_ENGINE`              | `MEDIA_RENDER_ENGINE`                  | per-source override possible       |
| `CLAUDE_TTS_VOICE`               | `MEDIA_RENDER_VOICE`                   |                                    |
| `CLAUDE_TTS_EDGE_VOICE`          | `MEDIA_EDGE_VOICE`                     |                                    |
| `CLAUDE_TTS_OPENAI_MODEL`        | `MEDIA_OPENAI_MODEL`                   |                                    |
| `CLAUDE_TTS_OPENAI_PYTHON`       | `MEDIA_OPENAI_PYTHON`                  |                                    |
| `CLAUDE_TTS_REALTIME_PYTHON`     | `MEDIA_REALTIME_PYTHON`                |                                    |
| `CLAUDE_TTS_DROP_DIR`            | `MEDIA_DROP_DIR`                       | legacy intake only                 |
| `CLAUDE_TTS_LONG_THRESHOLD`      | *(removed)*                            | single stream path                 |
| `CLAUDE_TTS_ENABLED`             | `MEDIA_ENABLED`                        |                                    |
| `AAR_STREAM_HOST`                | `MEDIA_STREAM_HOST`                    |                                    |
| `AAR_MOPIDY_DUCK_VOLUME`         | `MEDIA_DUCK_VOLUME`                    | now route/ policy                  |
| `RELAY_TTS_DROP_BIN`             | *(removed)*                            | CLI moves into media               |
| `RELAY_TTS_STREAM_BIN`           | *(removed)*                            | as above                           |
| `RELAY_LOG_FILE`                 | `MEDIA_LOG_FILE`                       |                                    |
| `RELAY_ENV_FILE`                 | `MEDIA_ENV_FILE`                       |                                    |

A `media migrate-settings` command performs the rename on
`~/.claude/settings.json` and `~/.config/agent-audio-relay.env`,
writing `.bak` copies first.

## Rollback story

- Phases 0–2 leave AAR running alongside the new core. Revert is a `git revert`
  + restart of the old services.
- After Phase 3, the legacy `$PREFIX/tmp/` socket paths are gone. Rollback past
  this point requires reinstating those symlinks (one-line shell command,
  documented in this file at execution time).
- After Phase 5, `tmux-voice-bridge` no longer exists as an external repo
  reference. Rollback would require restoring from the `packages/voice-bridge/`
  subtree merge commit.

## Open / deferred

- Lyrics-as-messages convergence (treat speech as a Mopidy track with lyrics):
  deferred until after Phase 6. The new shape doesn't preclude it.
- Local Whisper STT: `transcribe/` has the slot; implementation deferred.
- Snapcast targets in sink-speech: interface supports them from Phase 2;
  wiring to snapclient-mel/sp4r deferred until a use case forces it.
- Voice identity per Claude persona (sam vs david): policy table supports it
  from Phase 3; default mapping picked at Phase 6.
