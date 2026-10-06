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

## Voice and notifications — read from the code (2026-10-06)

Files: `packages/server/src/server/session/voice/` (`voice-turn-controller.ts`,
`voice-session.ts`), `packages/server/src/server/push/`,
`packages/protocol/src/agent-attention-notification.ts`.

**Turn-taking.** The server runs a VAD (a Silero ONNX model ships in the repo)
over PCM streamed from the client, plus a streaming STT session. States are
idle / listening / capturing. `speech_started` opens a turn; `speech_stopped`
commits the STT segment and starts a 10 s timer for the final transcript, firing
with whatever arrived if it times out. Segments are assembled in order, with
low-confidence flags.
- **Filler partials ignored.** A partial that is only "uh/um/hmm/oh…" does not
  count as speech content.
- **Empty final = false positive.** If the final transcript is empty, nothing is
  aborted and the session returns to idle.
- **One STT reconnect per turn,** then it gives up for that turn.

**Barge-in.** Interruption fires on the VAD's *confirmed speech start*, not on a
transcript ("so interruption does not wait for transcription"). It aborts the
abort-controller, cancels pending TTS playbacks, drops buffered audio segments,
and interrupts the running voice agent. The latency is logged as
`barge_in.llm_abort_latency`. Playback is confirmed by the client
(`confirmAudioPlayed`), so the server knows what was actually heard.
Our side: our barge-in and heard note already cover similar ground; the
two ideas to compare are (a) VAD-confirmed start as the interrupt trigger
rather than any audio energy, and (b) the filler-only filter.

**Notifications.** The daemon builds an "attention" payload with a reason of
`finished`, `error` or `permission`. The body is the assistant's last message
with markdown stripped (links keep their text, fences/headings/lists removed)
and truncated to 220 chars; permission requests carry kind
(tool/plan/question/mode). Delivery is Expo push in batches of 100, and tokens
answering `DeviceNotRegistered` are revoked. The payload carries
serverId/workspaceId/agentId, and tapping routes straight to that agent's tab.
So their push goes through Expo's service, not their own relay.
Ours: the stripped, 220-char preview and the three-reason split are the
borrowable bits for Sasonica notifications; the Expo dependency is not.

## Still open

- Real phone UX for stacked permission prompts (needs the app, not the code).
- What voice mode does about echo (TTS leaking back into the VAD); not seen in
  the files read.
