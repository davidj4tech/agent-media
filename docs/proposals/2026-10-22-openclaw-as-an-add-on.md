# OpenClaw as an add-on, not a core

David, 22 Oct 2026, after [delegated authority](2026-10-22-delegated-authority.md):
"I'm also curious about using something like OpenClaw under the hood", then
"probably better as an add-on rather than a core feature", including
**personas and some of its many features**.

Status: **add-on shelved, ideas kept** (David, 22 Oct 2026: "I'm not sure
that it's worth the effort at the moment. It's a lot of work and adds a lot
of overhead for probably not much gain. We could take some of the ideas from
it though."). OpenClaw is never a dependency of core or of delegated
sessions; the `intake-openclaw` add-on below is written down so it need not
be worked out again, not because it is planned. What carries forward is the
"Borrow the idea" rows of the feature table.

## What there is

- **OpenClaw 2026.6.11** is installed on red5 (`~/.npm-global/bin/openclaw`,
  state in `~/.openclaw`). Its gateway has been **off since 9 Aug 2026**
  (`openclaw-gateway`, `openclaw-tmp-watch`) to free about 537 MB on a
  memory-tight red5. HA voice moved to `voice_bridge`
  ([handover](../handover/2026-08-09.md)). Restore:
  `systemctl --user link ~/agent-config/harnesses/openclaw/systemd/user/openclaw-gateway.service`.
- Before that, the voice path was HA Assist / Matrix → OpenClaw →
  agent-media's MCP tools ([channels.md](../reference/channels.md), "Voice").
- It still holds three agents, **main**, **bel** and **soni**, each with a
  workspace of `SOUL.md`, `IDENTITY.md`, `MEMORY.md`, `DREAMS.md`,
  `HEARTBEAT.md` and `AGENTS.md`.
- **Personas have already moved out.** `~/agent-personas` imported Bel from
  `workspace-bel` (`personas/bel.md`, private source `private/bel.sops.md`),
  and has an `adapters/openclaw` render target. Sasonica offers personas
  through `persona-render` ([personas.py](../../packages/server/src/agent_media_server/personas.py),
  [persona scope](2026-10-17-persona-scope.md)).
- The add-on shape exists: `intake-hermes` is an optional package that speaks
  another agent host's replies through core, and Hermes, pi and opencode
  appear as agents in the app.

## Why not under the hood

- **One trusted operator per gateway.** OpenClaw's docs say its multi-user
  mode is session ownership and presence, not authorisation between users;
  for isolation they say to run separate gateways or hosts. Delegated
  sessions need the opposite: a separate identity and token for each turn
  and an agent with no shell.
- **Built around full access.** Shell, browser and files are its point. A
  delegated session would have to remove nearly all of it, leaving a larger
  attack surface than headless Claude plus our own MCP proxy. Its gateway has
  needed hardening before (the local WebSocket hijack, CVE-2026-25253).
- **Secrets in the workspace.** The old workspaces hold plaintext credential
  files the agent can read. Any revival should move those into OpenClaw's
  `secrets` (SecretRef) store first, or remove them.
- **It duplicates Sasonica.** sessiond, device pairing, the canvas, Matrix
  rooms as threads and the mautrix gateways cover most of what it offers. Its
  channels would compete with Matrix as the channel hub: two logins on one
  WhatsApp account, and two places for a message to land.
- **Memory.** red5 is still tight.

## The add-on

An optional package, `intake-openclaw`, in the shape of `intake-hermes`:

1. **Speech.** A hook on OpenClaw's final turn output (`hooks`) that hands
   the reply to core's `submit_event`, so OpenClaw agents speak like any
   other.
2. **Threads.** OpenClaw sessions on `/targets` with `source: "openclaw"`,
   read from its `sessions` / `transcripts` commands or the gateway API,
   reply through `openclaw agent` or the gateway. The same list, capture and
   send verbs as the tmux, herdr and Matrix sources.
3. **Off by default.** Nothing in core imports it. A host without OpenClaw
   simply has no OpenClaw rows. Installing it never starts the gateway.
4. **Refused in delegated and shared sessions,** like pi and Hermes, because
   it cannot be confined to the speaker's tokens.

## Personas: one source, OpenClaw as a render target

`~/agent-personas` stays the single source of truth. OpenClaw is one more
adapter, as Claude, pi, Hermes and SillyTavern already are:

- `persona-render <id> --adapter openclaw` writes the persona into an
  OpenClaw agent's workspace (`SOUL.md`, `IDENTITY.md`), rather than OpenClaw
  owning the persona and agent-personas copying from it.
- Persona scope applies unchanged: `render.compatible_adapters` decides
  whether a persona is offered for OpenClaw, and a private persona gets its
  private render only on a trusted model. An OpenClaw agent backed by an
  untrusted model gets the public shell.
- **What OpenClaw has that agent-personas does not:** per-persona `MEMORY.md`,
  `DREAMS.md` (memory consolidation) and `HEARTBEAT.md` (what to do on a
  timer). Those are state, not identity, so they stay in the OpenClaw
  workspace and are not copied back. Whether Sasonica wants persona memory
  of its own is a separate question (below).

## Its other features: bridge, borrow, or leave

| Feature | Verdict | Why |
|---|---|---|
| Channels (WhatsApp, Telegram, Signal, Slack, Discord, iMessage) | **Leave** | Matrix + mautrix is the channel hub. One login per network. |
| Matrix account | **Leave** | sessiond already reads and writes the rooms. A second Matrix client is how events get lost. |
| Multiple agents, each with a workspace | **Bridge** | Each OpenClaw agent becomes a row on `/targets`. |
| Personas (`SOUL.md`, `IDENTITY.md`) | **Bridge, one way** | Rendered from agent-personas (above). |
| Memory, dreams | **Borrow the idea** | Consolidating a persona's memory over time is worth designing natively, tied to Hippocampus and the existing memory helpers, not OpenClaw's files. |
| Heartbeat, cron | **Borrow the idea** | Scheduled turns for a session ("check the bookings at 9") fit sessiond. The `/schedule` style is already familiar. |
| Commitments (inferred follow-ups) | **Borrow the idea** | Close to Org TODOs. A commitment found in a thread could become an Org capture with `:SESSION:`. |
| Tasks and flows | **Leave for now** | Overlaps Org and sessiond. Revisit if a flow engine is needed. |
| Skills | **Bridge if cheap** | Many OpenClaw skills are prompts plus tools. Claude skills already cover ours. |
| Nodes (paired devices that run commands) | **Leave** | Sasonica's devices and `agent-phone-adb` cover it, with stricter rules for the phone. |
| Sandbox containers | **Borrow the idea** | The same shape as [Roles](2026-10-18-roles.md) option (c), a container per member chat. |
| Canvas, A2UI widgets | **Leave** | The canvas carries the reply's words ([decision](../decisions/2026-09-21-canvas-carries-words.md)). |
| ACP-backed coding agents | **Leave** | sessiond runs the coding agents directly. |
| `migrate` (import from other agent systems) | **Note** | Useful once, if someone moves from OpenClaw to Sasonica. |

## Order of work

The add-on is not planned. The gateway stays off.

**Worth doing anyway (housekeeping):** move or remove the plaintext
credentials in the old `~/.openclaw/workspace-*` directories, and check that
nothing in `bel` or `soni` exists only there and not in agent-personas.

**The ideas, each its own native proposal when wanted** (none depends on
OpenClaw):

1. **Scheduled turns** (from heartbeat and cron): a session can be woken on a
   timer or a schedule with a standing instruction, run headless in
   sessiond, and reply into its own thread. Most likely first: it is small
   and useful on its own.
2. **Commitments to Org** (from inferred follow-ups): a promise or task
   spotted in a thread is offered as an Org capture with `:SESSION:`, never
   filed silently.
3. **Persona memory** (from `MEMORY.md` and dreams): what a persona keeps
   between sessions, and a periodic consolidation pass, built on the
   existing memory helpers.
4. **A container per member chat** (from sandboxes): already
   [Roles](2026-10-18-roles.md) option (c); OpenClaw's setup is a reference
   when that is built.

**Only if OpenClaw comes back:** the speech hook, then rows and reply, then
the persona render target checked against `adapters/openclaw`, as described
under "The add-on".

## Open questions

- Should Bel and Soni run anywhere other than OpenClaw now that agent-personas
  renders them for Claude, pi and Hermes?
- Persona memory: per persona, per person, or per persona-and-person? It
  matters once sessions are shared.
