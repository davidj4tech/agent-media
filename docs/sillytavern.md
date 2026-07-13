# SillyTavern as an agent-media front-end — design note

Status: **active** (promoted from "later" 2026-07-11 — personas wanted). Slots
into the front door beside OWUI. The OWUI bridge already produced the two seams
SillyTavern reuses (the `tts-shim` for voice+canvas, the `/input` bridge), so
this is mostly *wiring*, not new capability. Mounted at `personas.<host>` via
`deploy/frontdoor.Caddyfile`.

## Why

SillyTavern is a richer conversational front-end than OWUI — persistent
characters/personas, world-info, group chats, streaming, expression sprites. As
an agent-media surface it gives the agent a **persona + memory shell** and a
polished mobile/desktop chat, while agent-media keeps owning voice + canvas.

## What it reuses (already built for OWUI)

- **Output / voice → `tts-shim`.** SillyTavern's TTS extension speaks the
  OpenAI-compatible API. Point it at `http://<host>:8782/v1` and it voices
  through agent-media's engines + canvas — the *same* shim OWUI Call mode uses.
  No new code.
- **Input → the `/input` injection path.** SillyTavern can call a custom
  "generate" endpoint (or a proxy) per turn. The same `POST /canvas/input`
  bridge the `intake-owui` Pipe uses would carry a SillyTavern turn into the
  agent session. An `intake-sillytavern` shim would look like `intake-owui`.

## The one genuinely different piece

SillyTavern expects a **chat-completions** backend (`/v1/chat/completions`,
streaming). Two ways to satisfy it:

1. **Real LLM backend, agent-media as voice/canvas skin** (easy, mirrors
   "conversational Call mode"): SillyTavern talks to a real model; the shim +
   an expression/visual hook give it agent-media's voice and canvas. The
   persona lives in SillyTavern; the agent-media stack is the A/V layer.
2. **agent-media session as the backend** (harder): a `/v1/chat/completions`
   adapter that injects the user turn into a live Claude Code session and
   streams the reply back. This is the synchronous-agent problem noted in the
   `tts-shim` README — real work, deferred.

## Sketch

```
SillyTavern (persona, world-info, chat UI)
   ├─ chat-completions → [real LLM]  ← option 1 (skin)  OR
   │                     [agent-media session adapter]  ← option 2 (later)
   └─ TTS (OpenAI API) → tts-shim :8782 → agent-media voice + canvas
```

## Next actions when picked up

- [ ] Stand up SillyTavern (docker) on the tailnet; TTS → `tts-shim` (verifies
      the shim already covers it end-to-end).
- [ ] Decide option 1 vs 2 for the reply source.
- [ ] If it earns its own adapter, add `packages/intake-sillytavern` modelled on
      `intake-owui`.
- [x] Expression sprites ↔ canvas — **built** (2026-07-11). The tts-shim shows
      a persona's portrait on the canvas when its `voice` has a portrait dir
      (`MEDIA_PERSONA_DIR/<slug>/neutral.png` + optional expression variants);
      canvas serves them at `/persona/<slug>/<file>` with a `portrait` purpose.
      Lexical emotion pick for now — next: feed ST's own expression classifier
      (or a model) instead of the keyword heuristic, and consider driving the
      sprite from HA/mood rather than just the reply text.
