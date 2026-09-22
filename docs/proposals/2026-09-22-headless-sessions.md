# Proposal: headless sessions (22 Sep 2026)

Status: **steps 0–4 built (22 Sep 2026), behind `MEDIA_HEADLESS`, off by
default.** Step 0 is the spike (`notes/2026-09-22-headless-spike.md`); what
was built for steps 1–4, and where it departs from this text, is in
[§12 As built](#12-as-built-22-sep-2026), after §11. Steps 5–8 are not
started. It sits under the server contract (`docs/server-contract.md`, §17
there) and the package split (`proposals/2026-09-21-server-package.md`). It
changes how the server reaches a session. It does not change what the app
sees, beyond a few additive fields.

## The short version

Today every session the app talks to is a terminal program in a tmux (or
herdr) pane. The server types into it with `send-keys` and reads it with
`capture-pane`. Everything else is inferred from the screen: whether the agent
is working, which dialog it is stopped on, the ghost prompt, whether a message
was actually submitted. That works on red5 because David's tmux setup is
there, and because a great deal of code has been written around the TUI's
quirks.

Claude Code, Codex, pi and Hermes each have a programmatic interface as well.
Through it a program sends messages, receives structured events, answers
permission requests and interrupts a turn, with no screen involved. This
proposal adds that path next to the pane path:

- A **`Driver`** seam in `agent_media_server`, with two implementations:
  `pane` (today's code, wrapped unchanged) and `headless` (one adapter per
  harness).
- A small **session host** service (`media-sessiond`) that owns headless agent
  processes, so they survive canvas restarts.
- **Chats started from the app go headless. Desk sessions stay in panes.** Both
  appear in the same thread list, and an idle session can be moved from one
  kind to the other.

The first step is a spike: one headless Claude session driven end to end,
measuring what the docs promise before anything is built on it.

---

## 1. Why

### What the pane coupling costs

The mechanisms that exist only because the agent is a TUI are listed in
Appendix B, with the files they live in. In summary:

- **Discovery** by walking `/proc` for `TMUX_PANE`, plus a pane registry
  written by a SessionStart hook. A fresh Claude's id is in nobody's argv, so
  `/ask` can come back with `session: null`.
- **State and dialogs read off the screen by regex.** This breaks at phone
  width (34 columns cut "esc to interrupt" to "· e…") and on scrolled dialogs.
  It cannot answer multi-select or free-text asks at all.
- **Typing that has to be checked:** literal-then-Enter with a 50 ms beat,
  waiting for the screen to settle, re-pressing Enter until the composer
  empties, refusing to send while text is half-typed, and flattening newlines
  so a message is not submitted in halves.
- **Opening a window** needs an attached tmux client. That means a holder
  process in a transient systemd unit, a 45 s wait for the TUI to paint, and an
  auto-answer for the resume modal.
- **Speech, titles, the ghost prompt and rename** are all keyed by, or scraped
  from, the pane.

Every item is code that works, and much of it was paid for with a bug. The pane
path stays for desk sessions. The point is that none of it is needed when the
agent is not a TUI.

### The product reason

Sasonica is meant for other people (the umbrella doc). Someone installing it
will not have David's tmux layout, amux registrations, `p-<dir>` session names,
a SessionStart hook writing a pane registry, or a desk client attached at all
times. Today a phone-started chat needs every one of those to exist. A headless
session needs the agent binary, its login, and a directory.

---

## 2. The seam: `Driver`

"Driver" is used rather than "transport" because the wire is not the part that
differs. What differs is who holds the agent process and how its state becomes
known. The pane driver learns state by looking at a screen, and the headless
driver is told it in events.

```python
class Driver(Protocol):
    kind: str                      # "pane" | "headless"
    caps: Caps                     # what this driver can do for this harness

    def start(self, agent: str, cwd: str, text: str, *, session: str = "",
              flags: Flags = ...) -> Started      # a fresh session with its first message
    def resume(self, session: str) -> Opened      # bring an ended session back, saying nothing
    def send(self, session: str, text: str) -> Sent   # multi-line text as given
    def interrupt(self, session: str) -> Interrupted
    def state(self, session: str) -> State        # working | waiting | approval | ended, + since
    def pending(self, session: str) -> Approval | None
    def answer(self, session: str, approval_id: str, decision: Decision) -> Answered
    def suggestion(self, session: str) -> str
    def rename(self, session: str, title: str) -> str
    def close(self, session: str) -> Closed
    def subscribe(self, session: str) -> Iterator[Event]   # for the §11 watcher
```

`Caps` is how the routes stay honest per harness: `interrupt`,
`structured_approvals`, `multi_select`, `free_text_answers`, `live_events`,
`suggestions`. Stop answering `interrupted: false, why: "not supported for pi"`
(contract §12) becomes `caps.interrupt == False`, not a special case.

Transcripts are not part of the driver. The conversation log is built from
agent-media's own speech history and manifests (`book_tracks.conversation_log`),
and both drivers feed those the same way (§6). The harness's transcript file is
the same file whichever driver wrote it (§A.1).

**Which driver owns a session.** The session host keeps a record per headless
session (`~/.local/state/agent-media/sessiond/<session>.json`: agent, cwd,
driver, pid, state). A session with a record there, whose process is live or
which is idle-parked, is headless. Anything found by the `/proc` walk in a pane
is a pane session. A session that is neither is *ended*, and the route that
revives it chooses the driver (§8).

### How the contract maps onto it

| Route / feature | Pane driver (today) | Headless driver | Simpler? |
| --- | --- | --- | --- |
| `GET /targets`, `/sessions/state` | `/proc` walk + a capture per pane, cached 3 s | the host's records + its last event, no capture | yes. No cache needed |
| `GET /conversation/log` `working` | activity hooks | same hooks, or `assistant` / `tool_use` events | same |
| `approval` | `parse_dialog` off the screen | the pending `can_use_tool` request, verbatim | **much**. Exact tool, input, question text, all options |
| `suggestion` | ghost scrape, else Haiku follow-up | `prompt_suggestion` event (`--prompt-suggestions`) | yes. The follow-up hook becomes a fallback |
| `POST /reply` | compose → send-keys → settle → re-press Enter | one JSON line on stdin | **much**. Newlines survive; `submitted` is known, not guessed |
| `POST /ask` (new) | hold a client, open window, wait ≤ 45 s, wait for the registry | spawn with `--session-id <uuid>` chosen up front | **much**. `session` is never `null` |
| `/session/resume` | window + resume modal | spawn with `--resume`; `-p` shows no modal | yes |
| `/session/close` | `kill-pane` | end stdin, then SIGTERM after a grace period | same |
| `/session/answer` | digit + Enter, verify 3 s, 504 | `control_response` echoing the `request_id` | **much**. Multi-select and free text become possible |
| `/rename` | type `/rename` | `/rename <title>` as a message (documented in `-p`), or the name agent-media already keeps | same |
| `/session/stop` (§12) | Escape + watch | `interrupt` control request; the receipt lists queued messages | yes, and exact |
| per-thread SSE (§11) | poll and diff every 1–3 s | push from the event stream; the diff watcher stays for pane sessions | yes |
| `/focus` | switch the desk client | not applicable; the app offers "open at the desk" (§7) | — |

The contract changes are additive:

- Thread rows and `/conversation` gain `driver: "pane" | "headless"`. `pane`
  is `null` for headless sessions, which the v0 clients already handle for
  ended sessions.
- `approval` gains `id` (the request id), `kind` (`"tool" | "ask" | "plan"`),
  `tool`, `input` (trimmed) and, for asks, `questions` in AskUserQuestion's own
  shape. `key` and the numbered `options` stay, so v0 clients keep working.
- `POST /session/answer` accepts `{session, id, decision}` next to today's
  `{session, choice, key}`. `decision` is `{"allow": true}`,
  `{"allow": true, "remember": true}` (echo the `localSettings` suggestion),
  `{"allow": false, "message": "…"}`, or `{"answers": {question: label |
  [labels] | free text}}` for an ask. This closes the §16 gap on multi-select
  and free-text answers.
- `/reply` stops flattening for headless sessions. The quote rides as a
  separate paragraph instead of `Re: "…" —`.

---

## 3. The headless driver, per harness

### Claude Code

Two ways to drive it:

1. **The CLI directly:** `claude -p --input-format stream-json
   --output-format stream-json --verbose --permission-prompt-tool stdio
   --prompt-suggestions [--include-partial-messages]`, with the process held
   open. User messages are JSON lines on stdin. Events are JSON lines on stdout.
   Permission requests arrive as `control_request` / `can_use_tool` and are
   answered with a `control_response` carrying the same `request_id`. Interrupt
   is a `control_request` with subtype `interrupt`.
2. **The Agent SDK** (Python `claude-agent-sdk`, whose wrapper is MIT but which
   bundles the Claude Code CLI; TypeScript `@anthropic-ai/claude-agent-sdk`, under
   Anthropic's commercial terms). It does exactly (1) for you: it spawns the CLI
   with those flags. The `--permission-prompt-tool stdio` flag was read from the
   SDK's own source on red5 (Meridian's copy, 0.2.141), not from the docs.

**Recommendation: (1), from Python, standard library only.** The SDK's main
job would be bundling a second Claude Code that lags the installed one
(Meridian's bundled CLI is 2.1.232; the desk runs 2.1.278). The parts of the
wire protocol we need are documented well enough to speak directly, and the
Python SDK stays as the fallback. The reasoning is in Appendix C.

What the headless session gets for free, and replaces:

| Today | Headless |
| --- | --- |
| registry wait for the id | `--session-id <uuid>` chosen by us |
| resume modal | none: the "Resume from summary" dialog is interactive only (it still costs a full reprocess of a cold, >100k session; see §9) |
| classify | `system/init` → working on each user message → `result` → waiting; `control_request` → approval |
| ghost prompt | `prompt_suggestion` after each turn |
| `/commands` (a throwaway `-p` run today) | the live session's own `system/init.slash_commands`, and the initialize response's `commands` |
| AskUserQuestion read off the screen | `can_use_tool` with `tool_name: "AskUserQuestion"` and `input.questions`, answered with `updatedInput: {questions, answers}` |
| interrupt by Escape | `interrupt` control request, whose receipt says which queued messages will still run |

Two documented details shape the design. Permission prompts in `-p` "don't time
out": a pending `can_use_tool` holds the turn until it is answered. And the
initialize response carries `pending_permission_requests`, so a client that
reconnects to a live process gets its unanswered prompts again (Claude Code
2.1.268+).

### Codex, pi and Hermes

Details are in Appendix C. In short: **Codex** has the richest API,
`codex app-server`, a JSON-RPC server (marked experimental) with threads,
turns, interrupt, steer and structured approval requests. **pi** has `--mode
rpc`, with JSON lines, prompt, steer and abort, and no approvals by design.
**Hermes** speaks ACP natively (`hermes acp`), with cancel and
`request_permission`. The Agent Client Protocol could cover all four through
adapters. The recommendation is native adapters for Claude and Codex, ACP for
Hermes (its native interface) and RPC for pi. Appendix C gives the reasons.

---

## 4. Who owns the process

A headless session's stdin is its only way in. If the canvas held it, every
deploy would kill every chat, since the canvas is restarted after each pull on
red5 and on p8a. So the processes belong to a separate service.

**`media-sessiond`**: a systemd user unit, standard library only, rarely
changed, and restarted only when its own code changes.

- It spawns and holds each headless agent, reads its stdout and writes its
  stdin. The environment is today's `open_window` environment: `env -u
  ANTHROPIC_API_KEY`, `PATH` from `harnesses.program`, cwd from the request.
- The canvas talks to it over a unix socket
  (`$XDG_RUNTIME_DIR/agent-media/sessiond.sock`, mode 0600), with JSON lines:
  `start`, `send`, `interrupt`, `answer`, `close`, `subscribe`, `list`.
- It appends every event to
  `~/.local/state/agent-media/sessiond/<session>.events.jsonl`, trimmed the way
  `activity.py` trims its files. This one log serves as the SSE source, the
  debugging record ("what did the agent actually send?"), and the replay after
  a canvas restart.
- Its record per session, the ownership file of §2, lets it re-adopt nothing
  after a crash. A dead process is simply *parked* (next item).

**Idle shutdown.** A session that is `waiting` with no subscriber for
`MEDIA_SESSIOND_IDLE` (default 30 min) has its stdin closed and is marked
parked. The transcript is on disk. The next `send` respawns it with
`--resume <id>` and delivers the message. The user sees one slower reply. This
mirrors Claude's own background-session supervisor, which stops a process idle
for about an hour. The default is shorter here because the phone, not a desk,
is the usual client, and memory on red5 is the tighter limit.

A session parked with a permission request pending cannot keep the request
alive. Options: never park a session in `approval`, or use the documented
PreToolUse `defer` decision. With `defer`, the process exits with `stop_reason:
"tool_deferred"` and the call is re-offered on resume; it works in `-p` only
and only for a single tool call per turn. **Start with "never park on an
approval"** and consider `defer` later.

**If the service itself restarts,** its children die with their stdin, and
each in-flight turn is lost: SIGTERM leaves it "unfinished", and a later
resume continues it. That is acceptable because the service rarely changes,
but it is the reason to keep it small. The design rules out holding stdin in
something that survives the service (a FIFO per session, or a transient unit
per session like `hold_client`) until restarts prove to be a problem.

**Limits.** At most `MEDIA_SESSIOND_MAX` (default 4) live processes. Starting
a fifth parks the least-recently-used idle one, and if none is idle it
refuses with a sentence for the phone. Memory per process is measured in the
spike, not guessed here.

**p8a.** The phone runs its own canvas, but agents run on red5. The phone's
canvas does not host headless sessions. If that ever changes it is the same
service, installed by `media-setup` under the host's role.

---

## 5. Permissions and approvals

This is the largest change in kind. Today an approval is text on a screen that
we hope we parsed right. Headless, it is a structured request that stays
pending until answered:

```json
{"id": "req_…", "kind": "tool", "tool": "Bash",
 "input": {"command": "git push", "description": "Push the branch"},
 "title": "Claude wants to run git push", "reason": "…",
 "suggestions": [{"type": "addRules", "destination": "localSettings", …}]}
```

The app renders it natively. The answer goes back as a `control_response` with
the same id, so the "question changed" 409 becomes an id mismatch and the 504
disappears. An ask (`kind: "ask"`) carries AskUserQuestion's questions (one to
four, each with two to four options, `multiSelect`), and the app can offer
"Other" with free text. Both are documented answer shapes.

**Permission mode for phone-started chats.** Phone chats inherit amux scratch's
`--dangerously-skip-permissions` today. Part of the reason is that approving
from the phone was fragile. With structured approvals, headless chats can run
in `auto` (David's `defaultMode`) or `acceptEdits`, and ask the phone for the
rest. The mode is a per-place setting, and the default is decided in the spike
review, not here.

**One approvals store, three producers.** The session host writes pending
requests to a small store the server reads. It is keyed by id, and each entry
records who is waiting on it. Sasonica Shell's phone approvals
(`sasonica-shell/docs/tools-and-approvals.md` §2) have the runner ask
agent-media locally and wait. They become a second producer into the same store
and the same `/session/answer` path, with the runner's row as the thread.
Nothing new is needed for the phone to approve a shell row beyond what a
headless session already needs.

**The same path for pane sessions, later.** Claude's `PermissionRequest` hook
fires in interactive sessions too, and can answer allow/deny with
`updatedInput`. A blocking hook that posts the request to the store and waits
for a short while would give desk sessions structured phone approvals, with
the terminal dialog as the fallback when nobody answers. This is worth a
separate proposal once the store exists. It would retire `parse_dialog` for
Claude panes entirely.

---

## 6. Speech

Hooks from `settings.json` do run under `-p`. The docs say so in several
places: the PermissionRequest, MessageDisplay and async-hook teardown notes, and
`--include-hook-events` exists for them. So the Stop, PreToolUse
(AskUserQuestion), UserPromptSubmit and activity hooks keep producing speech,
steps and asks for headless sessions with no change. Four things differ:

1. **No `TMUX_PANE`.** `submit.py` already falls back to the session id for the
   queue identity when there is no pane. The per-tmux-session voice falls back
   to the default voice. The session host should set `MEDIA_SESSION_VOICE`, or
   a `MEDIA_SOURCE_KIND=headless` plus `MEDIA_SOURCE_PROJECT=<cwd basename>`
   pair, so the per-project voice and filing survive. Filing a conversation
   under its workspace reads `source_tmux_session` off its turns today. For
   headless sessions it takes the project from the cwd, which is what "What a
   chat can be pointed at" already argued for.
2. **Highlight-in-pane** has no pane. It is off for these sessions, which is
   correct.
3. **Async hook teardown.** In `-p`, Claude Code kills async hooks still
   running when the process exits. The Stop hook already detaches playback
   into a `setsid` child, so speech survives. Idle shutdown (§4) should still
   wait until the last Stop hook has handed off.
4. **Meridian runs headless Claude under the same `settings.json`.** Its
   thousands of `sdk-ts` sessions in `~/.claude/projects/-home-ryer--meridian`
   have no activity files, which suggests the hooks exit early or never ran
   for them. This has not been checked. Our headless sessions *must* speak,
   and Meridian's must not. The spike confirms which case each is in, and the
   session host marks its own sessions (`MEDIA_HEADLESS=1`) so the hook never
   has to guess.

**Later: speech fed from the event stream.** With `--include-partial-messages`,
the session host sees the reply as it is written, before Stop fires. Feeding
intake from the stream could start speech sentences earlier, and phone TTFA is
the bridge's round trips (memory: speech latency is the bridge). But it
duplicates what the Stop hook does: dedup, asks, the follow-up line. Keep the
hooks for phase 1 and measure before moving anything.

---

## 7. The desk

A headless session is not in a terminal. That is a loss for David, who reads
sessions at the desk. Three answers, in order of cost:

1. **Watch it in the canvas or the app.** The event log carries everything the
   terminal would show, and more structured. A read-only "live" view on the
   canvas page is cheap once §11's stream exists.
2. **Take it to the desk when idle.** `claude --resume <id>` in a terminal works
   on `-p` sessions by id; they are only left out of the picker and
   `--continue`. The docs warn that the same session resumed in two processes
   interleaves into one transcript, and nothing locks against it. So "move to
   desk" is a server action: the session host parks the session (after the
   current turn, never mid-approval), and then `send.open_window(resume=True)`
   opens it in a pane. That is today's revive, whose duplicate-process check
   already refuses a second copy. From then on the pane driver owns it.
3. **Bring it back to the phone.** The reverse, for an idle pane session:
   `/session/close` (the pane goes, the transcript stays), and the next phone
   message resumes it headless. The app shows this as "continue on phone".

Neither handoff happens implicitly. A reply to a pane session types into the
pane, and a reply to a headless one goes to the host. Switching is a button,
because the two kinds behave differently (approvals, the terminal view).

**Which desk (22 Sep 2026, built).** "The desk" above is David's: a tmux
session per project, amux, and a SessionStart hook that files panes into
them. That is now one of two layouts (server-contract.md §18,
`agent_media_core/layout.py`), detected from the amux directory plus that
hook and written to config.toml at install. On a fresh host the layout is
`default`: app chats are headless, and whatever still needs a pane (the
"move to desk" revive, a harness without a headless driver) opens as a
window in one tmux session, `sasonica`, with a client held on it. "Open at
the desk" there means `tmux attach -t sasonica`. Nothing in this section's
handoffs changes; only where the pane lands.

**Claude's own background sessions** (`claude --bg`, `claude agents`, `claude
attach`) look like the missing middle: a supervisor-owned process you can
attach any terminal to. But they have no documented way to *send* a message
without attaching, and permission prompts wait for an attached terminal. So
they cannot serve the phone. They are relevant in two ways. `claude agents
--json` is a documented, scriptable list of running sessions with a
`state`/`status`, which the pane driver could read instead of classifying the
screen. And on red5 that list currently holds 908 `background` entries from
`~/.meridian`, all `blocked`, so any listing must filter. **Remote Control**
(`claude remote-control`) is Anthropic's own way to drive a local session from
the Claude app. It is a fine tool for David, but it is Anthropic's client, not
Sasonica's, so it is not a transport for us.

---

## 8. Defaults

| Where the session starts | Driver |
| --- | --- |
| `/ask` "new chat" from the app | **headless** (`MEDIA_ASK_DRIVER`, default `headless` once the spike passes) |
| A reply to an ended session that last ran headless | headless |
| A reply to an ended session that last ran in a pane | pane if a desk client is attached (today's revive), else headless |
| `branch` | the driver of the thread it branched from |
| Started at the desk (amux, a terminal) | pane, as today |
| `/harnesses/run` (install, login) | pane, as today: those *are* terminal programs |

Both kinds appear in one list. `/targets` rows gain `driver`. Titles for
headless sessions come from agent-media's own name (`book_tracks.rename`), then
Claude's generated title from the transcript, then the first prompt. This is
the same order `_live_title` uses for Codex and pi.

The fallback in the third row is what makes the product work for a user without
tmux: nothing on the phone path requires a multiplexer.

---

## 9. Risks and unknowns

| Risk | Why it matters | Mitigation |
| --- | --- | --- |
| **Subscription auth for a distributed app.** The Agent SDK overview says: "Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK." | For David's own use, `claude -p` under his own login is Claude Code run by its owner (as Meridian already does). For Sasonica shipped to others, driving *their* installed `claude` under *their* login is close to what the note forbids, and the note says to use API keys. | **Unresolved; decide before shipping headless to anyone else.** Read the Consumer and Commercial terms, and ask Anthropic if unsure. Keep the pane driver as the path that never touches the question: the user runs Claude Code themselves, and we only type. Support API-key auth for headless as a setting |
| The control protocol is only partly documented | The envelope of `can_use_tool` and `interrupt` messages is read from the SDK, not a spec | Feature-detect with `system/init.capabilities`; pin shapes in a test against recorded bytes; fall back to the Python SDK |
| Two processes on one session | Interleaved transcript, which is documented and not locked | One owner per session: the host's record plus the existing running-check in `open_window`; handoffs park first |
| Cost of resume after idle | A cold resume of a >100k session reprocesses the whole history (the interactive modal offers a summary instead; `-p` does not) | Idle timeout tuned against the prompt-cache lifetime; offer `/compact` from the app; measure in the spike |
| Hooks behave differently under `-p` | Async hooks killed at exit; `-p` loads project hooks in untrusted folders; `--bare` would skip hooks entirely | Never `--bare`; only `/targets` places; delay idle shutdown for Stop |
| Codex app-server is experimental | Protocol churn | Pin the generated JSON schema in tests; keep panes for Codex until it settles |
| Memory on red5 | N resident agent processes | `MEDIA_SESSIOND_MAX`, idle parking, measure in the spike |
| The desk loses sight of phone chats | David reads sessions in tmux | The canvas live view; "move to desk" |
| Meridian's sessions in shared listings | Transcripts and `claude agents` fill with non-conversations | Everything is keyed by the host's own records, never by listing |

---

## 10. Plan

Each step is its own commit, with the contract test green after each.

**0. Spike (a day, nothing merged).** A throwaway Python script, run by hand in
a scratch directory under `/tmp`, never against a real session. It drives one
`claude -p` stream-json process end to end and records every byte. It answers:

Appendix D lists the nine questions it answers, from latency and memory to hooks, interrupt, approvals and auth. The write-up goes in `docs/notes/`. Proceed only if queueing, interrupt, approvals and
hooks behave as the docs say.

**1. The seam, no behaviour change.** `drivers/pane.py` wraps today's
`send`/`sessions` functions behind `Driver`. The routes call the driver, and
`test_contract.py` passes unchanged.

**2. `media-sessiond` + the Claude adapter,** behind `MEDIA_ASK_DRIVER=headless`
(default off). `/ask`, `/reply`, `/session/resume|close`, `/sessions/state`,
`/targets` and `/conversation/log` work for headless sessions. `driver` is added
to the contract.

**3. Structured approvals.** The approvals store, `approval.id/kind/input/
questions`, and `/session/answer {id, decision}`. The Nuxt app keeps its
numbered-option UI, and the assistant-ui app gets native tool UIs.

**4. Stop (§12) and the per-thread stream (§11),** built on the host's events for
headless sessions and on the diff watcher for panes. This means doing §11/§12
*after* step 2, not before, because the headless half is the easy half.

**5. Handoffs:** "move to desk" and "continue on phone".

**6. Flip the default** for app-started chats to headless.

**7. Codex app-server adapter.** Then pi RPC. Then Hermes ACP. Each harness
gets its own `caps`.

**8. Later, separate proposals:** PermissionRequest-hook approvals for pane
sessions; speech from the event stream; `claude agents --json` as the pane
driver's state source.

---

## 11. What each harness offers today

"Verified" means read in current docs, in a generated schema, or in local
`--help` / source on red5 on 22 Sep 2026. Nothing was run live.

| | Claude Code 2.1.278 | Codex 0.155.1 (`app-server`) | pi 0.84.1 (`--mode rpc`) | Hermes 0.17.0 (`acp`) |
| --- | --- | --- | --- | --- |
| Long-lived, multi-turn | yes: stream-json stdin (docs) | yes: `turn/start` per turn (schema) | yes (rpc.md) | yes (source) |
| Streamed events | yes: `assistant`/`user`/`result`, partials, hook events (docs) | yes: `item/*` deltas (schema) | yes: `message_update`, tool events (rpc.md) | yes: `session/update` (source) |
| Session id chosen up front | `--session-id` (help) | no, server assigns it (schema) | `--session-id` (help) | no (source) |
| Interrupt | `interrupt` control request, with receipt (docs + SDK source) | `turn/interrupt`, also `turn/steer` (schema) | `abort`, `steer` (rpc.md) | `cancel` (source) |
| Structured approvals | `can_use_tool` via `--permission-prompt-tool stdio` (flag from SDK source; shapes from docs) | three `requestApproval` requests (schema) | none by design (README) | `request_permission` (source) |
| Structured asks | AskUserQuestion via the same path, `answers` map (docs) | `item/tool/requestUserInput` (schema; not examined) | extension UI only (README) | clarify (source; unverified) |
| Next-prompt suggestion | `prompt_suggestion` (docs) | — | — | — |
| Resume | `--resume <id>` (docs) | `thread/resume` (schema) | `--session`, `switch_session` (rpc.md) | `load_session`/`resume_session` (source) |
| Open in the TUI afterwards | yes, by id, when not running (docs) | via `--remote` to the daemon (help); plain `codex resume` **unverified** | likely, same directory (**unverified**) | same `state.db` (**unverified**) |
| Subscription login | yes for own use (`-p` without `--bare`); **restricted for third-party products** (docs) | ChatGPT login or API key (README) | subscription or API key (providers.md) | provider config (**unverified**) |
| Licence | Claude Code: Anthropic terms; Python SDK wrapper MIT; TS SDK: Commercial Terms | Apache-2.0 | MIT | MIT |

---

## 12. As built (22 Sep 2026)

Decisions from David the same day: go ahead after the spike; **phone-started
chats run headless with a stricter permission profile** (his settings allow
`Bash(*)`/`Write(*)`, which must not apply there), switchable per host back
to normal settings "if it gets too busy"; desk sessions stay in panes;
subscription login with `ANTHROPIC_API_KEY` stripped.

| Step | State | Where |
| --- | --- | --- |
| 1. The seam | **done** | `driver/__init__.py`, `driver/pane.py`; `send.py`'s gated entry points dispatch; every pane test unchanged |
| 2. media-sessiond + Claude adapter | **done**, behind `MEDIA_HEADLESS` | `sessiond.py`, `driver/headless.py`; `media sessiond`; unit template `packages/core/services/media-sessiond/` (not installed) |
| 3. Structured approvals | **done** (no separate store — sessiond keeps them) | `driver/headless.py` `approval_of`, `/session/answer` structured form; strict profile `permissions.py` |
| 4. Stop and the stream | **stop done** less the speech marker; the §11 stream reads headless state and is nudged by sessiond's event counter | `stop.py`, `thread_events.py` |
| 5. Handoffs | not started | |
| 6. Flip the default | not done — `MEDIA_HEADLESS` stays off | |
| 7–8 | not started | |

**Deviations from the text above.**

- **The flag** is `MEDIA_HEADLESS=1` (not `MEDIA_ASK_DRIVER`). It turns on
  headless for every fresh Claude session `/ask` starts, and makes the
  readers list sessiond's sessions. The spike's child marker became
  `MEDIA_SOURCE_KIND=headless` (plus `MEDIA_SOURCE_WORKSPACE`), so the one
  name does not mean two things.
- **Permissions (§5).** Not a per-place mode: a per-host profile,
  `MEDIA_HEADLESS_PERMISSIONS=strict|normal`, default strict. Strict cannot
  drop the user settings (`--setting-sources ""` silences the hooks, spike
  change 8), so it keeps them and overlays `--settings` whose `ask` rules
  name every non-read-only tool and **mirror every user and project `allow`
  rule** — Claude Code evaluates deny → ask → allow — plus `--permission-mode
  default`. Checked for real: a project `allow: Bash(*)` did not pre-approve
  a Bash call. The cost: a harmless `ls` asks the phone too; a sessiond-side
  auto-allow for read-only shell commands was left out as too easy to get
  wrong.
- **Approval shape (§2, §5).** `kind` is `"tool"` or `"question"` (not
  `"ask"`/`"plan"`; ExitPlanMode arrives as an ordinary tool request).
  Fields follow spike change 7: `id`, `tool`, `display_name`,
  `input_summary`, `input` (trimmed), `description`, `blocked_path`,
  `tool_use_id`, `suggestions`, `questions`. The v0 `question`/`options`/
  `key` stay alongside. The answer is `{session, request_id, decision:
  "allow"|"deny", answers?, message?}` rather than `{id, decision: {…}}`;
  `remember` (echoing a suggestion) is not built.
- **No approvals store** yet: sessiond is the only producer, and keeps the
  pending requests itself (it had to — the CLI does not re-send them).
- **Restarts (§4).** A pending request does not survive a sessiond restart;
  it is recorded as `lost` and answering it is a 409 with `code: "lost"`.
  The next message resumes the session and the model, seeing its tool call
  interrupted, asks again if it still wants it. At start-up sessiond ends
  any child a crashed instance left (matched by `MEDIA_SESSIOND_SESSION` in
  its environment).
- **Idle parking** measures idleness from the last event; there is no
  "no subscriber" condition. Never parked on an approval, while working, or
  with queued messages.
- **`/reply`** no longer flattens for headless sessions and sends the quote
  as a Markdown quote paragraph; `/ask` still flattens (its parse needs one
  line).
- **Titles (§8)**: the shelf's name, then Claude's (`custom-title`, then the
  last `ai-title`), then the first message.
- **Suggestions**: the Haiku follow-up, as the spike concluded; nothing reads
  `prompt_suggestion`.
- **Stop (§12 of the contract)**: built through the drivers; the
  per-session speech marker is not, so there is no cutoff and this thread's
  queued replies are not dropped.
- **`claude agents --json`** does list headless sessions (seen in the smoke
  run); nothing reads it.
- **`/rename`** of a headless session keeps the name on the shelf and
  answers `terminal: false` (nothing is typed; sending `/rename` as a
  message was not tried).
- **The socket** lives under `$XDG_RUNTIME_DIR`, which systemd user services
  have and runit services do not (the phone): there it falls back to the
  state dir, and both the canvas and sessiond must agree — set
  `MEDIA_SESSIOND_SOCKET` if they run under different managers.

**The smoke run** (22 Sep 2026, haiku, Claude Code 2.1.278, silent:
`--setting-sources project,local` so no user hooks, plus
`MEDIA_HOOK_ENABLED=0`; a project `allow: ["Bash(*)", "Write(*)"]` in the
scratch cwd; driven through `driver/headless.py` and an in-process sessiond):

| | measured |
| --- | --- |
| `start` call (spawn, write, wait for the CLI's ack) | 1.86 s |
| start → first `result` ("pineapple") | 3.63 s (`ttft_ms` 1638) |
| idle RSS | 270 MB |
| warm `send` call / → `result` ("mango") | 0.01 s / 1.37 s |
| send → approval on the thread (Bash `touch`, despite `allow: Bash(*)`) | 1.86 s |
| `answer` allow → `result` ("done", file created) | 1.46 s |
| `interrupt` mid-tool → receipt and `aborted_tools`, state `waiting` | 0.11 s |
| `close` → process gone (record `closed`) | 0.91 s (exit code 1: the last turn was the interrupted one) |

To switch it on for a host: `MEDIA_HEADLESS=1` in `~/.config/agent-media.env`
(optionally `MEDIA_HEADLESS_PERMISSIONS`, `MEDIA_SESSIOND_IDLE`,
`MEDIA_SESSIOND_MAX`, `MEDIA_HEADLESS_MODEL`), `media-setup
install-services media-sessiond` (the role file requires `origin` and the
flag), then restart the canvas so it reads the flag.

---

## Appendix A — evidence

### A.1 Claude Code

- `-p`, stream-json, partial messages, `system/init` fields and
  `capabilities`, `api_retry`, SIGTERM vs SIGINT semantics, `--permission-prompts
  none`, slash commands in `-p`, and `--resume` across directories:
  https://code.claude.com/docs/en/headless
- Transcripts at `~/.claude/projects/<project>/<id>.jsonl` for every entrypoint;
  `-p` and SDK sessions left out of the picker and `--continue` but resumable by
  id; two processes on one session interleave; the resume-from-summary dialog
  (Pro/Max, over an hour idle and over 100k tokens); permission mode on
  resume: https://code.claude.com/docs/en/sessions
- `canUseTool`, the AskUserQuestion input and `answers` output, free text, "the
  callback can stay pending indefinitely", and `defer` as the alternative:
  https://code.claude.com/docs/en/agent-sdk/user-input
- `interrupt()` receipt, `interrupt_receipt_v1` / `interrupt_cancel_queued_v1`,
  a client "that drives the CLI's control protocol directly",
  `pending_permission_requests` on initialize, the `requestId` echo, "permission
  prompts don't time out", and `prompt_suggestion`:
  https://code.claude.com/docs/en/agent-sdk/typescript
- PreToolUse `defer` (`-p` only, single tool call, `stop_reason:
  "tool_deferred"`); PermissionRequest input and decision; AskUserQuestion under
  `-p` needs a permission host; async hooks killed at `-p` teardown:
  https://code.claude.com/docs/en/hooks
- The subscription note, and the Commercial Terms for the SDK:
  https://code.claude.com/docs/en/agent-sdk/overview
- Background sessions, the supervisor, `claude agents --json` states, and no
  send without attaching: https://code.claude.com/docs/en/agent-view
- Remote Control: https://code.claude.com/docs/en/remote-control
- Local: `claude --help` 2.1.278 (`--session-id`, `--permission-prompts`,
  `--prompt-suggestions`, `--replay-user-messages`, `--bg`, `agents`, `attach`).
  The SDK in Meridian's `node_modules` (0.2.141, bundling CLI 2.1.141) has
  `"--permission-prompt-tool","stdio"`, `control_request` / `can_use_tool` /
  `interrupt` / `set_permission_mode`, and a `LICENSE.md` pointing to
  Anthropic's legal terms. The Python SDK repo's `LICENSE` is MIT.
  `~/.claude/sessions/<pid>.json` carries `status` (`busy` / `idle`) for
  interactive sessions, an internal file.

### A.2 Codex

- `codex exec --json` events: https://learn.chatgpt.com/docs/non-interactive-mode
- App-server: https://learn.chatgpt.com/docs/app-server. Method and request
  names come from `codex app-server generate-json-schema` on red5 (0.155.1).
  `codex mcp-server` no longer exists in 0.155.1. There is `codex queue`, and
  the TUI's `--remote`.
- Licence and auth: https://github.com/openai/codex

### A.3 pi and Hermes

- pi: the installed docs `rpc.md`, `json.md`, `sdk.md` and `README.md` (0.84.1,
  now `@earendil-works/pi-coding-agent`).
- Hermes: `hermes acp --help` and `acp_adapter/` in `~/.hermes/hermes-agent`;
  https://hermes-agent.nousresearch.com/docs/user-guide/features/acp;
  https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration
- ACP: https://agentclientprotocol.com/protocol/overview,
  https://agentclientprotocol.com/overview/agents,
  https://github.com/agentclientprotocol/codex-acp,
  https://github.com/svkozak/pi-acp

### A.4 Not verified

- The exact JSON envelope of `can_use_tool`, `control_response` and `interrupt`
  on a raw CLI stdin (the shapes come from the SDK's types and source, not a
  protocol spec).
- Whether a message sent mid-turn over stream-json is queued or merged. The
  interrupt receipt implies queueing; not observed.
- Whether the Stop hook actually speaks for a `-p` session on red5, and why
  Meridian's `sdk-ts` sessions leave no activity files.
- Memory per idle headless Claude process.
- That `claude -p --resume` never shows the "Resume from summary" dialog. The
  docs describe the dialog as something shown before your first message, and a
  `-p` run has no dialogs, but they do not state it outright.
- That subscription login is acceptable for driving a *user's* Claude Code from
  a distributed Sasonica. The docs' note reads against it; this is a terms
  question, not a technical one.
- Codex: that a plain `codex resume` opens an app-server thread; approval
  defaults under app-server; `requestUserInput` shape.
- pi and Hermes: that RPC/ACP sessions open cleanly in their own TUIs; two
  processes on one session; Hermes auth details; whether `pi-acp` supports
  cancel and permissions.
- Whether the 908 `blocked` background entries from `~/.meridian` in `claude
  agents --json` are Meridian's doing or leftovers of something else.

## Appendix B — the pane mechanisms

These mechanisms exist only because the agent is a TUI in a pane. File
references are to `packages/server/src/agent_media_server/` unless noted.

| Mechanism | Where | Why it exists |
| --- | --- | --- |
| Finding sessions by walking `/proc` for `TMUX_PANE` / `HERDR_PANE_ID`, plus the SessionStart pane registry and its pid checks | `sessions.live_sessions`, `session_of_pane` | A session's address is its pane, and a fresh Claude's id is in nobody's argv. `/ask` can return `session: null` when the registry row is more than 10 s late |
| Working / waiting / approval read off the screen by regex | `panes.classify_cc`, `classify` | The TUI is the only place the state is shown. A 34-column pane cuts "esc to interrupt" to "· e…", and a working session then showed as waiting (memory: narrow pane hides "working") |
| `/sessions/state` cached for 3 s | `sessions.session_states` | Each poll is a `/proc` sweep plus one `capture-pane` per pane |
| Dialogs parsed from the screen, keyed by a sha1 of their text | `sessions.parse_dialog`, `approval_for` | Permission prompts and AskUserQuestion exist only on screen. A long list scrolls (`partial`), the question can be off the top, and multi-select and free-text answers cannot be given at all (contract §16) |
| Answering by pressing a digit and Enter, then watching for 3 s | `send.answer` | There is no other way in. It returns 504 when the keys went nowhere |
| Literal-then-Enter with a 50 ms beat | `panes.send` | Claude Code's input buffering drops an Enter that arrives with the text |
| Waiting for the screen to stop changing before typing | `send._settle` | The TUI takes text before it takes an Enter |
| Pressing Enter again until the text leaves the composer | `send._ensure_submitted`, `_unsent`, `core conversation.submit` | Long replies lost their Enter. It returns 502 `submitted: false` when it gives up |
| Refusing to send while something is half-typed | `sessions.pane_draft` | Typed text is appended to whatever is already in the box |
| Flattening newlines and quotes to one line | `send.compose` | A newline typed into the pane would submit half the message |
| Needing an attached tmux client, and holding one in a transient systemd unit via `script` with `SHELL=/bin/sh` | `send.attached_session`, `hold_client` | Claude Code's TUI will not start without a client. The zsh `=word` expansion, drifted holders and the after-new-session respawn hook each cost a debugging session |
| Waiting up to 45 s for the TUI to paint | `send.open_window`, `pane_ready` | Text typed before the TUI is up is lost |
| Auto-answering the "Resume from summary" modal | `send.pane_ready` | A revived window otherwise sits on the modal forever |
| Scraping the ghost prompt from dim SGR runs, falling back to a Haiku follow-up when it is cut with "…" | `sessions.ghost_prompt`, `suggestion_for`, `core intake/_followup.py` | The suggestion exists only on screen, drawn to the pane's width |
| Titles from the terminal title with the spinner stripped | `sessions._pane_titles` | That is where Claude writes its name |
| Renaming by typing `/rename` | `send.send_rename` | The same channel as everything else. It is refused while something is being typed |
| Stop is planned as "Escape, then watch the pane" | contract §12 | The same channel again, with a rule against pressing Escape on a dialog |
| Speech keyed by pane: voice per tmux session, the queue identity, highlight, filing a conversation under its tmux session | `core intake/hook_claude_code.py`, `intake/submit.py` (`source_pane`) | Hooks run inside the pane and inherit `TMUX_PANE` |

## Appendix C — the harness adapters

### Claude: CLI or SDK

Why the raw CLI rather than the Agent SDK:

- agent-media is Python and stdlib-first. The SDK would add a dependency whose
  main job is bundling a *second* Claude Code, one that lags the installed
  version. Meridian's bundled CLI is 2.1.232 while the desk runs 2.1.278, and
  its sessions are stamped `entrypoint: sdk-ts`. `cli_path` can point the SDK at
  the installed binary, but then the SDK is a thin layer over a protocol we can
  speak ourselves.
- The parts of the protocol we need are documented as wire shapes, not only as
  SDK methods. The TypeScript reference describes "a client that drives the
  CLI's control protocol directly" setting `cancel_queued` on the `interrupt`
  control request, and says a `control_response` sent "outside the SDK" must
  echo `request_id`. `system/init` advertises `capabilities`
  (`interrupt_receipt_v1`, `interrupt_cancel_queued_v1`) so features can be
  detected rather than inferred from the version.
- What is *not* documented is the full envelope of each control message. The
  spike records the actual bytes, and the adapter is written against those.
  Keep the Python SDK as the fallback if the raw protocol proves brittle.

### Codex

`codex app-server` (marked experimental) speaks JSON-RPC over stdio, a unix
socket or a websocket, with `thread/start`, `thread/resume`, `turn/start`,
`turn/interrupt`, `turn/steer` and streamed `item/*` notifications. Approvals
come as server requests (`item/commandExecution/requestApproval`,
`item/fileChange/requestApproval`), answered `accept`, `acceptForSession`,
`decline` or `cancel`. This is a better fit than `codex exec --json`, which is
one prompt per process. 0.155.1 also has a shared app-server daemon and a TUI
flag `--remote unix://…` that attaches a terminal to the daemon's thread. That
is a real "move to desk" for Codex, and it is unverified here.

Watch item: `codex migrate-rollouts` and `~/.codex/thread_history_1.sqlite`
suggest the JSONL rollouts that `core harnesses.py` reads are on their way out.

### pi

`pi --mode rpc`: JSON lines on stdin and stdout, with `prompt`, `steer`,
`follow_up`, `abort`, `get_state`, `get_messages`, `switch_session`, and events
from `agent_start` to `agent_end`. There are no approvals by design. It takes
`--session-id`, as the pane path already relies on. The framing is strict LF,
so a line reader that splits on other breaks is wrong.

### Hermes

`hermes acp`: Agent Client Protocol over stdio, with `new_session`,
`load_session`, `resume_session`, `prompt`, `cancel`, and approvals through
`request_permission`. There is also the TUI's own JSON-RPC gateway, but that is
internal to Hermes.

### Is ACP the common protocol?

The Agent Client Protocol (agentclientprotocol.com) covers all four: Hermes
natively, and Claude (`claude-agent-acp`, on the TypeScript SDK), Codex
(`codex-acp`, on the app-server) and pi (`pi-acp`, on `--mode rpc`) through
adapters. One client for four harnesses is tempting. The cost is a Node
adapter process per Claude session running the TypeScript SDK under its
licence and its bundled CLI, another layer in every bug, and lowest-common-
denominator features: `interrupt_receipt`, `prompt_suggestion` and
`pending_permission_requests` have no ACP equivalent. **Recommendation:**
native adapters for Claude and Codex, the two that matter. Hermes goes through
ACP because that is its native interface. pi stays on RPC. Revisit ACP if a
fifth harness arrives.

## Appendix D — spike questions

A throwaway script in a scratch directory under `/tmp`, never against a real session. It records every byte.

1. Time from spawn to `system/init`, and to the first `assistant` event. Resident
   memory while idle.
2. Several messages over one stdin, including one sent mid-turn. Is it queued,
   and does it merge into the turn?
3. `interrupt` mid-tool: the receipt, and whether the session takes the next
   message normally.
4. One `Bash` permission request answered allow and one answered deny. One
   AskUserQuestion with multi-select plus free text, answered structurally.
5. Hooks: does the Stop hook speak? What is `TMUX_PANE`? Is the activity file
   written? The same check against a Meridian-style `sdk-ts` session, to explain
   its silence.
6. `prompt_suggestion`: does it arrive, and how soon after `result`?
7. Transcript location; `claude --resume <id>` in a terminal once the headless
   process has exited; a headless `--resume` of a TUI session.
8. Kill the process mid-turn, then resume: what comes back.
9. Auth: it runs on the subscription login with `ANTHROPIC_API_KEY` stripped,
   as the pane path does.
