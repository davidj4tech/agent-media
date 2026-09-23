# Landscape watch list

Projects near agent-media / Sasonica worth checking for ideas. The weekly
`landscape-watch` run reads this file, looks at what changed in each since the
last digest, and writes `docs/landscape/YYYY-MM-DD.md`. It proposes additions
at the end of each digest; move the good ones in here by hand.

Each entry: repo or URL, then what we care about in it.

## Agent phone / remote UIs

- slopus/happy — mobile + web client for Claude Code and Codex; session
  handoff, push notifications, voice, E2E-encrypted relay.
- siteboon/claudecodeui — web/mobile UI over Claude Code and Cursor CLI
  sessions; file tree, git panel, session browser.
- anomalyco/opencode (was sst/opencode) — terminal agent with a client/server split; its web, desktop
  and remote clients, and how they share a session.
- omnara-ai/omnara — "mission control" for agents from the phone; approvals and
  questions as notifications.
- amantus-ai/vibetunnel — terminal sessions in the browser; how it proxies a
  PTY and what the mobile experience does.
- Anthropic's own Claude Code on web / mobile / remote control — changelog
  items that overlap Sasonica (remote sessions, notifications, voice).

## Voice / TTS for agents

- mbailey/voicemode — two-way voice conversations with Claude Code over MCP;
  VAD, barge-in, local Whisper/Kokoro.
- Claude Code TTS hook projects generally — anything that speaks the Stop
  hook's output; how they handle interruption, pacing and long replies.

## What to look for

Ideas we could borrow: interaction patterns on a phone, how asks/approvals are
surfaced, voice turn-taking and barge-in, pairing and transport (tunnels,
relays), session lists, notifications. Not star counts or marketing.
