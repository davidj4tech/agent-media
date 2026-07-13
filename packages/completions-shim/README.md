# agent-media-completions-shim

Makes a **live Claude Code session answer as an OpenAI chat model**, so Open
WebUI — hands-free **Call mode** included — can converse *with* your agent:
inject the user turn, wait for the reply in the session transcript, stream it
back in OpenAI format.

It's the synchronous sibling of the `intake-owui` Pipe:

| Use as OWUI's model | Behaviour | Call mode? |
|---|---|---|
| `intake-owui` Pipe | inject + return an ack; reply comes back async via agent-media | dictation only |
| **completions-shim** (this) | inject + **wait for the reply** + stream it back | ✅ conversational |

## How it works

```
OWUI (Call): STT → POST /v1/chat/completions
      │
      ▼
completions-shim :8783
  1. resolve target pane (/agents)     3. poll /peek until a new turn lands
  2. POST /input  (inject the turn)        AND /agents shows the session idle
      │                                 4. strip [[visual:]]/markdown
      ▼                                 5. stream sentences back (OpenAI SSE)
  the Claude session answers ───────────┘   → OWUI feeds them to the tts-shim
```

Everything goes through the canvas HTTP surface (`/input`, `/agents`, `/peek`) —
no second reply-capture path.

## ⚠️ Voice: avoid double-speak

The target session **already speaks** (agent-media, room-routed). In Call mode
OWUI + the tts-shim voice the reply *on the device*. Both = double speech. Fix:

- **Mute the session.** A muted pane still renders its canvas figure and records
  history — it just doesn't speak aloud. `MEDIA_COMPLETIONS_MUTE=1` (default)
  runs `media mute-pane --pane <p> on` for you. **Dedicate a session to OWUI.**
- **Keep `MEDIA_SHIM_CANVAS` OFF** for this model, so the session's own hook owns
  the canvas figure (else the shim draws a second one).

Net: session draws the figure + stays silent · OWUI + tts-shim voice it. One
voice, one figure.

## OWUI setup

1. Run it: `media-completions-shim` (or the systemd unit). Set the target:
   `MEDIA_COMPLETIONS_TARGET=tmux:%5` (or `amux:owui`, or a session name).
2. OWUI → Admin → Settings → Connections → add an **OpenAI** connection:
   Base URL `http://127.0.0.1:8783/v1`, any API key (or `MEDIA_COMPLETIONS_API_KEY`).
   The `agent-media` model appears — pick it.
3. TTS → the **tts-shim** (see that package), `MEDIA_SHIM_CANVAS=0`.
4. Open Call mode. Talk → the agent answers → it speaks in agent-media's voice →
   mic reopens.

## Config

| Env | Default | Meaning |
|-----|---------|---------|
| `MEDIA_COMPLETIONS_TARGET` | — (required) | `tmux:<pane>` · `amux:<name>` · `<session-name>` |
| `MEDIA_COMPLETIONS_CANVAS` | `http://127.0.0.1:8781` | canvas base URL |
| `MEDIA_COMPLETIONS_TOKEN` | — | amux token for `/input` (blank if canvas trusts the tailnet) |
| `MEDIA_COMPLETIONS_TIMEOUT` | `180` | max seconds to wait for a reply |
| `MEDIA_COMPLETIONS_SETTLE` | `2.0` | transcript must be stable + idle this long before "done" |
| `MEDIA_COMPLETIONS_MUTE` | `1` | ensure the target pane is muted |
| `MEDIA_COMPLETIONS_PORT` / `_BIND` | `8783` / `127.0.0.1` | listen addr |
| `MEDIA_COMPLETIONS_API_KEY` | — | require `Authorization: Bearer <key>` |

## Known limits (verify against a live session)

- **Completion detection is heuristic** — "a new turn landed AND `/agents` state
  left `working` AND the transcript held stable for `SETTLE`s." A slow tool call
  that dips idle mid-turn could end the reply early; raise `SETTLE` if so.
- **Turn-granular, not token-streamed** — `/peek` yields whole transcript turns,
  so streaming is sentence-chunked *after* the reply completes, not live as
  Claude types. Fine for Call mode (it waits for the full turn anyway).
- **One conversation per target** — the shim drives one session; concurrent OWUI
  chats to the same target would interleave. Give each its own session/model.
