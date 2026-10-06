# Paseo — deep dive (2026-10-06)

[getpaseo/paseo](https://github.com/getpaseo/paseo) · [paseo.sh](https://paseo.sh) ·
TypeScript, ~19.6k stars, custom licence, pre-release v0.11.0-beta.5 (10-05) over
stable v0.10.3. Read from the README, `docs/` (architecture, permissions,
timeline-sync, hub) and the public voice page; I did not run it or read the
voice code, so voice turn-taking/barge-in is **not** established.

## What it is

One daemon on your machine (Node) spawns and manages agents (Claude Code via the
Agent SDK, Codex app-server, Copilot via ACP, OpenCode, Pi, others). Clients —
Expo mobile app, Electron desktop, web, CLI, TypeScript SDK — all talk to the
daemon over WebSocket, directly (TCP/Tailscale) or through an optional relay
with end-to-end encryption. Self-hosted, no telemetry, plugins in TypeScript.
Same shape as ours: phone UI over a daemon on the dev box.

## Ideas worth borrowing

1. **Live stream vs authoritative history** (`docs/timeline-sync.md`). Live
   `agent_stream` deltas are for immediacy; a separate paged fetch is the source
   of truth. A sequence gap triggers `fetch after cursor`, repeated until
   `hasNewer: false`; a watchdog guards lack of *progress*, not total time;
   retries back off 1 s → 30 s and reset on reconnect/visibility change. Maps to
   Sasonica's reconnect/catch-up and the relay's doorbell.
2. **Presence is not delivery.** Client heartbeat (device, app visible, focused
   agent) only routes *notifications*; it must never gate timeline delivery.
   Useful rule for our notify-vs-speak decision.
3. **Idempotent creation and send.** `idempotencyKey` on create, stable
   `messageId` on send, request IDs per attempt; reuse with different args is a
   conflict. Directly relevant to phone-sent turns retried over a flaky link
   (relay, roadmap item 16).
4. **Principals, credentials, single-use pairing.** A pairing invitation is an
   expiring, single-use exchange that creates a principal + credential;
   permissions are additive allows (`workspace.read/write`, `daemon.manage`…),
   owner/operator/viewer are UI presets only, delegation can only attenuate.
   Worth a look when the hosted relay needs more than one tenant device.
5. **Voice = a hidden agent session.** Voice mode runs a voice LLM through your
   existing provider as a hidden session that can launch and control other
   agents; dictation is separate. Local Parakeet STT + Kokoro TTS (ONNX), or
   OpenAI. Compare with our TTS-the-reply model: theirs is a conversational
   orchestrator, ours speaks the agent's own output.
6. **Bounded tool output** (64 KiB slice before it enters either path) so a
   reopen can't restore an oversized payload — cheap guard for our step list.

## Differences from us

- They orchestrate many agents in parallel with worktrees and a Hub for
  GitHub/Slack/Linear triggers; we are one chat/voice front-end.
- No sign of the ambient canvas, spoken-reply pacing or heard-note tracking.
- Their relay is optional and E2E; ours is a hosted per-tenant Durable Object.

## Open questions (need a real look)

- Barge-in and turn-taking in voice mode — the docs only say it exists.
- How push notifications are delivered without telemetry (their own relay?).
- Mobile UX for stacked permission/question prompts.
