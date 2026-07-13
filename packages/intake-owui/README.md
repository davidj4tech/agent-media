# agent-media-intake-owui

Open WebUI as a **voice/chat front door** to your agent. OWUI's mic (dictation)
and composer feed the agent; the reply comes back the agent-media way — voice +
canvas — not as text in the OWUI thread.

This is the **input half** of the OWUI↔agent-media bridge. It deliberately does
*not* touch the output side: OWUI's own TTS stays off, and agent-media keeps
owning the room-routed voice. (OWUI audio is browser-local; agent-media audio is
server-side — keeping them separate avoids double-speak.)

> Want a full **conversational** turn — inject *and* wait for the reply, in the
> OWUI thread, hands-free Call mode? That's the sibling `completions-shim`. This
> Pipe is fire-and-forget: it sends and acks, the reply arrives out-of-band.

## How it works

```
OWUI mic / composer
        │  (STT is OWUI's own, client-side)
        ▼
  agent-media Pipe  ──POST /input {text, target}──▶  canvas server (:8781)
  (OWUI Function)     X-Auth-Token: <amux token>       │  amux send / tmux
        │            ◀── {"ok", "detail"} ────────────┘  keystroke inject
   "↳ sent to <detail>"  (ack in thread)          the agent's session
```

The Pipe reuses the canvas's existing `/input` keystroke-injection endpoint, so
there's exactly one injection path — the same one the canvas's own touch UI and
the tmux popup use. The Pipe runs inside OWUI's server and reaches the canvas
directly at `127.0.0.1:8781`; this hop does **not** go through Caddy.

The canvas answers with `{"ok": bool, "detail": str}`: on success `detail` is the
resolved destination (`amux:foo`, the session name); on failure it's the reason
(`no speaker on record yet`, `unknown amux session …`). The Pipe echoes it into
the thread so a fire-and-forget turn still gets a meaningful ack.

## Setup

1. **Enable OWUI speech-to-text** (no code): OWUI → Admin → Settings → Audio →
   *Speech-to-Text Engine*. Whisper (local) or any OpenAI-compatible endpoint.
   The composer's 🎤 button now dictates; that's the input you're after.

2. **Install the Pipe**: OWUI → Admin → Functions → **+** → paste
   `owui/agent_media_pipe.py` → enable. It adds a model called **agent-media**.

3. **Fill the Valves** — run the helper on the canvas host:
   ```
   media-intake-owui pair     # prints canvas_url + amux_token + default_target
   media-intake-owui check    # confirms /healthz is up and /input is authorized
   ```
   Paste those into the Pipe's Valves (gear icon on the function).

4. **Use it**: pick **agent-media** as the model, then talk or type. Each turn
   is injected into `default_target` (`speaker` = the last-speaking agent, or
   `amux:<session>` for a specific one).

## Pipe Valves

| Valve | Default | Meaning |
|-------|---------|---------|
| `canvas_url` | `http://127.0.0.1:8781` | canvas server base URL (its `/input`) |
| `amux_token` | — | amux token from `pair`; blank iff `MEDIA_VISUAL_TRUST_TAILNET=1` |
| `default_target` | `speaker` | `speaker` (last-speaking agent) or `amux:<session>` |
| `acknowledge` | on | echo a one-line `↳ sent to …` into the thread |
| `timeout_s` | `8.0` | HTTP timeout to the canvas server |

## Helper env (`media-intake-owui`)

| Env | Default | Meaning |
|-----|---------|---------|
| `MEDIA_CANVAS_URL` | — | explicit canvas base URL (wins over everything) |
| `MEDIA_VISUAL_URL` | — | the `media` CLI's push target(s); first entry borrowed |
| `MEDIA_VISUAL_PORT` | `8781` | port for the loopback default URL |
| `AMUX_AUTH_TOKEN` | `~/.amux/auth_token` | amux token (env wins; `none` = dropped) |
| `MEDIA_VISUAL_TRUST_TAILNET` | off | `1` → canvas trusts the tailnet, no token needed |

## Scope / caveats

- **Dictation, not Call mode.** OWUI's Call mode wants to *speak* the assistant
  turn via OWUI TTS — the output side we left with agent-media on purpose. Use
  push-to-talk dictation (mic → composer → send). If you do enable Call mode it
  will only voice the short "↳ sent" ack. For a real conversational turn (wait
  for the reply, stream it back), use `completions-shim` instead.
- **Auth**: `/input` is keystroke injection, so it's token-guarded. `pair`
  reads `~/.amux/auth_token` (or `AMUX_AUTH_TOKEN`). The alternative is running
  the canvas with `MEDIA_VISUAL_TRUST_TAILNET=1`, in which case leave the
  `amux_token` valve blank.
- **No double-speak**: keep OWUI's TTS disabled so the only voice is
  agent-media's.

## Note: no daemon here

Unlike the two shims, this package ships no long-running server — the Pipe lives
inside OWUI's own process, and `media-intake-owui` is a one-shot pairing/health
helper. So there's no `systemd/` unit: nothing to keep running.
