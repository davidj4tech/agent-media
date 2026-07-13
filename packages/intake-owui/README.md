# agent-media-intake-owui

Open WebUI as a **voice/chat front door** to your agent. OWUI's mic (dictation
and Call mode) and composer feed the agent; the reply comes back the
agent-media way — voice + canvas — not as text in the OWUI thread.

This is the **input half** of the OWUI↔agent-media bridge. It deliberately does
*not* touch the output side: OWUI's own TTS stays off, and agent-media keeps
owning the room-routed voice. (See the "fault line" note in the design
discussion — OWUI audio is browser-local; agent-media audio is server-side.)

## How it works

```
OWUI mic / Call / composer
        │  (STT is OWUI's own, client-side)
        ▼
  agent-media Pipe  ──POST /input {text, target}──▶  canvas server (:8781)
  (OWUI Function)     X-Auth-Token: <amux token>       │  amux send / tmux
        │                                              ▼
   "↳ sent to speaker"  (ack in thread)          the agent's session
```

The Pipe reuses the canvas's existing `/input` keystroke-injection endpoint, so
there's exactly one injection path — the same one the canvas's own touch UI and
the tmux popup use. The Pipe runs inside OWUI's server and reaches the canvas
directly at `127.0.0.1:8781`; this hop does **not** go through Caddy.

## Setup

1. **Enable OWUI speech-to-text** (no code): OWUI → Admin → Settings → Audio →
   *Speech-to-Text Engine*. Whisper (local) or any OpenAI-compatible endpoint.
   The composer's 🎤 button now dictates; that's the input you're after.

2. **Install the Pipe**: OWUI → Admin → Functions → **+** → paste
   `owui/agent_media_pipe.py` → enable. It adds a model called **agent-media**.

3. **Fill the Valves** — run the helper on the canvas host:
   ```
   media-intake-owui pair     # prints canvas_url + amux_token + default_target
   media-intake-owui check    # confirms /input is up and authorized
   ```
   Paste those into the Pipe's Valves (gear icon on the function).

4. **Use it**: pick **agent-media** as the model, then talk or type. Each turn
   is injected into `default_target` (`speaker` = the last-speaking agent, or
   `amux:<session>` for a specific one).

## Scope / caveats

- **Dictation works now; full Call mode is a stretch.** OWUI's Call mode wants
  to *speak* the assistant turn via OWUI TTS — which is the output side we left
  with agent-media on purpose. Until the optional `/v1/audio/speech` shim exists,
  use push-to-talk dictation (mic → composer → send), not hands-free Call mode.
  If you do enable Call mode, it will only voice the short "↳ sent" ack.
- **Auth**: `/input` is keystroke injection, so it's token-guarded. `pair`
  reads `~/.amux/auth_token` (or `AMUX_AUTH_TOKEN`). The alternative is running
  the canvas with `MEDIA_VISUAL_TRUST_TAILNET=1`, in which case leave the
  `amux_token` valve blank.
- **No double-speak**: keep OWUI's TTS disabled so the only voice is
  agent-media's.
