# Handover: write down the Sasonica server contract (21 Sep 2026)

## Context

Decided today, in order:

- **Sasonica** is the umbrella name: app + server + link (Cloudflare Tunnel) +
  shell (Runlet). See `~/projects/runlet/docs/umbrella.md`.
- The phone app is being rebuilt: Capacitor 7 shell and our Java stay; the
  Nuxt 2 / ABS web layer is replaced by **assistant-ui** (React), built with
  **React Router v7 in SPA mode**. See `docs/app-redesign-options.md`.
- agent-media gets simplified around that. See `docs/simplification-plan.md`.
  **This session is step 1 of that plan.**

## The task

Write down the JSON/SSE contract between the app and agent-media, so the new
front end can be built against a spec instead of against `canvas.py`.

The server today is the `visual` package, chiefly
`packages/visual/src/agent_media_visual/canvas.py` and `reply.py`. Endpoints
in use include `/targets`, `/sessions/state`, `/sessions`, `/ask`, `/rename`,
`/conversation`, `/conversation/log`, `/conversations`, `/events`, `/input`,
`/input-claim`, `/stop`, `/say`, `/speech`, `/speech/now`, `/speech/ctl`.
Confirm the full list from the code; don't trust this one.

For each endpoint: method, auth (`MEDIA_SHARE_TOKEN` off-loopback), request
shape, response shape, SSE event types and payloads, error cases, and which
client uses it today (Sasonica app, canvas page, CLI). Mark anything
ABS-specific or canvas-only as not part of the app contract.

Bind it to assistant-ui's `ExternalStoreRuntime` (messages, `onNew`,
`onCancel`, `isRunning`, thread-list adapter, human tool UI for asks); the
mapping table is in `simplification-plan.md` step 4. Note the gaps: things
the runtime needs that no endpoint provides yet.

## Deliverables

1. `docs/server-contract.md` — the spec.
2. Contract tests that pin the response shapes (against the throwaway-server
   pattern in memory `canvas-headless-harness`; always route-block `/input`,
   never send real input to David's sessions).
3. A proposal, not yet done, for splitting the API out of `visual` into its
   own package (plan step 4 names this). Don't move code in this session.

## Read first

- `docs/handover/2026-09-09-sasonica-web-the-chat-page.md` and
  `2026-09-09-react-client-parity.md`: earlier work on a React chat page
  against these same endpoints. Reuse what it learned.
- Parallel sessions share this working tree: stage by path, never `git add -A`.
- No `Co-Authored-By` trailers.
