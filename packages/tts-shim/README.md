# agent-media-tts-shim

An OpenAI-compatible `/v1/audio/speech` endpoint that voices any chat front-end
through **agent-media's** engines — and, optionally, paints the reply onto the
shared canvas. Built for OWUI's hands-free **Call mode**; reusable by
SillyTavern and anything else that speaks the OpenAI TTS API.

## What it does

```
front-end (OWUI Call / SillyTavern)
      │  POST /v1/audio/speech {input, voice, response_format}
      ▼
  tts-shim (:8782)  ── strip [[visual:]] + markdown ──▶ render_text(engine)
      │                                                      │ audio bytes
      │  MEDIA_SHIM_CANVAS=1 → spawn_visual (canvas figure)  │
      ◀──────────────────── audio/mpeg ─────────────────────┘
```

Because it returns the **full audio**, Call mode's turn-taking works: the
front-end plays the utterance, knows when it ends, and reopens the mic. The
audio plays in the *caller's tab* — so unlike agent-media's room-routed speech,
this makes the front-end a **per-device sink** (headset on the PineNote while
the house stays quiet). `MEDIA_SHIM_CANVAS=1` still lights the shared canvas.

## Run it

```
media-tts-shim                 # 127.0.0.1:8782, engine from MEDIA_RENDER_ENGINE
# or install the unit:
systemctl --user enable --now agent-media-tts-shim
```

## Point OWUI at it (Call mode end-to-end)

OWUI → Admin → Settings → **Audio → Text-to-Speech**:

| Field         | Value                          |
|---------------|--------------------------------|
| TTS Engine    | OpenAI                         |
| API Base URL  | `http://127.0.0.1:8782/v1`     |
| API Key       | anything (or `MEDIA_SHIM_API_KEY`) |
| TTS Model     | `agent-media`                  |
| TTS Voice     | a real engine voice, e.g. `en-AU-NatashaNeural`, or blank for the default |

Then open **Call mode**. You talk (OWUI STT), your OWUI model answers, the shim
speaks it in agent-media's voice, the canvas illustrates, the mic reopens.

## The reply-source seam (read this)

Call mode voices whatever the OWUI **model** returns. Two coherent setups:

- **Conversational Call mode** — point OWUI's model at a real LLM (e.g. Claude
  via API), or at the **completions-shim** to talk to a live Claude Code
  session. The shim gives it agent-media's voice + canvas. Fully hands-free.
- **"Talk to my running agent"** — use the `intake-owui` Pipe as the model. It
  injects your turn into a live Claude Code session and returns an ack; the real
  reply comes back async via agent-media's own path. That flow is *not*
  Call-mode-conversational (Call mode would only voice the ack). Use
  push-to-talk dictation there, not Call mode.

> Pairing with the **completions-shim**: keep this shim's `MEDIA_SHIM_CANVAS`
> **off** so the driven session's own Stop-hook owns the canvas figure (else you
> get two). See that package's README.

## Config

| Env | Default | Meaning |
|-----|---------|---------|
| `MEDIA_SHIM_PORT` | `8782` | listen port |
| `MEDIA_SHIM_BIND` | `127.0.0.1` | bind address (tailnet IP for remote front-ends) |
| `MEDIA_SHIM_API_KEY` | — | require `Authorization: Bearer <key>` if set |
| `MEDIA_SHIM_CANVAS` | off | `1` → also spawn the canvas figure |
| `MEDIA_SHIM_SESSION` | — | pin all surfaces to one canvas scene; unset → per-**voice** continuity (each SillyTavern persona evolves its own artwork) |
| `MEDIA_SHIM_CANVAS_URL` | `http://127.0.0.1:8781` | canvas server the shim pushes persona portraits to |
| `MEDIA_PERSONA_DIR` | `~/.config/agent-media/personas` | persona portrait store — `<voice-slug>/neutral.png` (+ optional `happy/sad/angry/surprised`) |
| `MEDIA_PERSONA_EMOTION` | on | `0` → always `neutral` (skip the lexical expression pick) |
| `MEDIA_RENDER_ENGINE` | `edge` | agent-media engine to render with |
| `MEDIA_RENDER_VOICE` | — | fallback voice when the request names an OpenAI canned voice |

### Persona portraits (SillyTavern)

> **`voice` does double duty** — it's the engine render voice *and* the persona
> key. So set each SillyTavern character's TTS voice to a **real engine voice**
> (e.g. `en-US-AriaNeural`) and name its sprite dir by that voice's **slug**
> (`en-us-arianeural/neutral.png`). A friendly name like `Aria` fails to render.

When a request's `voice` names a persona that has a portrait directory, the
canvas shows that **character's face** (letterboxed, per-persona scene) instead
of a generated figure — the persona is *present* on the wall. A crude lexical
classifier swaps expression sprites (`happy`/`sad`/`angry`/`surprised`, else
`neutral`). A reply carrying an explicit `[[visual:]]` marker still wins — the
figure shows, not the face. No portrait dir for that voice → nothing changes,
you get the normal generated-figure path. Sprites are served by the canvas at
`/persona/<slug>/<file>`; the shim only pushes URLs.

> Note: `edge` emits mp3 regardless of `response_format`; keep OWUI's format on
> mp3 (its default). Non-edge plugin engines may honour other formats.
