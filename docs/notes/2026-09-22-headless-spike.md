# Headless sessions spike (22 Sep 2026)

The spike that step 0 of `docs/proposals/2026-09-22-headless-sessions.md` asks
for. One `claude -p` stream-json process was driven end to end by a stdlib
script, and every byte in both directions was recorded. Claude Code 2.1.278 on
red5, under David's Claude Max login.

- Driver: `spike/headless/harness.py`, one scenario per run.
- Raw logs: `spike/headless/logs/<scenario>-<n>.jsonl`. Each line is
  `{t, dir: in|out|err|note, obj}`, where `t` is seconds since spawn.
  `show.py` prints a compact timeline.
- Summary numbers: `spike/headless/results.json`.
- The logs went through `sanitize.py` before commit. It replaced the account
  e-mail, hook outputs (SessionStart injects memory excerpts) and thinking
  signatures. The envelope shapes are untouched.
- Cost: 23 runs, about 17 minutes of wall time. At API prices that would be
  $0.97, nearly all of it Claude Code's own ~32k-token system prompt being
  cached. Most runs used `--model haiku`: one on opus (TTFT) and one on sonnet
  (suggestions). The 5-hour window read 0.27 used during the spike.

**Verdict: proceed.** Queueing, interrupt, approvals and hooks all behave as
the docs say, and in several places better than the proposal assumed. There
were two misses. `prompt_suggestion` never arrived, and the reconnect replay
of pending permission requests did not happen on a same-pipe re-initialize.
Neither blocks step 2. The changes to the proposal are listed at the end.

## Appendix D, question by question

### 1. Latency and memory: verified

| | measured |
| --- | --- |
| spawn → `initialize` control_response | 1.7–2.2 s (3 runs) |
| `system/init` before any user message? | **no.** It is emitted when the first user message is read, and again at the start of *every* turn |
| cold spawn with the message sent at t=0 → `system/init` | 1.4–3.2 s (median ≈ 2.4 s, 17 runs) |
| cold spawn → first `stream_event` (`message_start`), haiku | 2.0–4.0 s |
| warm process, user message → `message_start`, haiku | 0.85–1.05 s |
| warm process, user message → `message_start`, **opus** | 4.3 s (the result's `ttft_ms` 4257; the prompt cache for opus was cold) |
| `--resume` from a new process → `system/init` / first text | 2.6 s / 3.7 s (haiku, a small session) |
| `result.ttft_ms` field | present on every result; 660–2500 ms for haiku |
| stdin closed → process exit | 0.6–7.2 s (it finishes async work first) |
| idle resident memory, one process | **100–220 MB RSS** (opus run 101–110 MB; haiku runs 180–219 MB), plus 7 MB swap. Single process `claude` with no children while idle |

red5 had 1.3 GB available and 8 GB of swap in use during the spike. At about
200 MB per session, `MEDIA_SESSIOND_MAX=4` costs about 0.8 GB, which is right
for this host.

### 2. Messages over one stdin, including one mid-turn: verified. They are queued, then merged into the running turn

A message written while a tool runs is queued. At the next tool boundary it is
**injected into the same turn**, and one `result` covers both messages
(`queue-2`: `"result": "A done\nbanana"`, `num_turns: 2`). A message sent
after `result` starts a new turn with a fresh `system/init`.

If the user message carries a client `uuid`, the CLI reports its fate
precisely. This is the `msg_lifecycle_v1` capability:

```json
{"type":"command_lifecycle","command_uuid":"a7709af5-…","state":"queued",…}     // t=7.64, mid-turn
{"type":"command_lifecycle","command_uuid":"a7709af5-…","state":"started",…}    // t=18.08, right after the tool_result
{"type":"command_lifecycle","command_uuid":"a7709af5-…","state":"completed",…}  // t=20.31
```

States seen: `queued`, `started`, `completed`, `cancelled`. Assistant messages
and results also echo `user_message_uuid` / `user_message_uuids`. So
`/reply`'s `submitted` becomes a known fact rather than a guess.

The user message we send (the `session_id: ""` is accepted, and the CLI fills
it in):

```json
{"type":"user","session_id":"","uuid":"<client uuid4>","message":{"role":"user","content":[{"type":"text","text":"…"}]},"parent_tool_use_id":null}
```

### 3. Interrupt mid-tool: verified, with a receipt

```json
→ {"type":"control_request","request_id":"req_7f45…","request":{"subtype":"interrupt"}}
← {"type":"control_response","response":{"subtype":"success","request_id":"req_7f45…","response":{"still_queued":["35e09916-…"]}}}   // 14 ms later
← {"type":"system","subtype":"task_notification","status":"stopped",…}
← {"type":"user","message":{"content":[{"type":"tool_result","content":"The user doesn't want to proceed with this tool use…","is_error":true,…}]}, "tool_result_meta":[{"non_execution_kind":"user-rejected"}]}
← {"type":"user","message":{"content":[{"type":"text","text":"[Request interrupted by user for tool use]"}]}}
← {"type":"result","subtype":"error_during_execution","is_error":true,"stop_reason":"tool_use","terminal_reason":"aborted_tools",…}
```

- The receipt lists queued messages **by the client `uuid`**. When messages
  are sent without one, both lists come back empty even though a message is
  queued (`interrupt-1`, `interrupt-2`). So the adapter must always send a
  `uuid`.
- Without `cancel_queued`, the queued message then runs as its own turn
  (`plum`). With `"cancel_queued": true` it is dropped, and the receipt says
  `{"still_queued":[],"cancelled":["1944b01b-…"]}` (`interrupt-3`).
- The next message after an interrupt is handled normally (`mango`, 0.7–2.4 s
  TTFT).
- Interrupting while the model is streaming, not in a tool, gives
  `terminal_reason: "aborted_streaming"` (`interrupt-0-bg-sleep`).

**Surprises:**

- A bare `sleep 30` is refused by Claude Code's own guard ("Blocked:
  standalone sleep 30 … use run_in_background"). The model then ran it in the
  background.
- When that **background task finished, 30 s later, the process started a new
  turn by itself**, with no user message: `task_notification` →
  `system/init` → a reply → a `result`. A headless session can therefore go
  from waiting to working with nobody sending anything. State has to come from
  events, not from "we sent a message".

### 4. Approvals: verified (Write and Bash allow/deny; AskUserQuestion with multi-select and free text)

`--permission-prompt-tool stdio` works on the raw CLI. The request as
received:

```json
{"type":"control_request","request_id":"34c79dca-81d5-47ff-a698-e9fd66575082",
 "request":{"subtype":"can_use_tool","tool_name":"Write","display_name":"Write",
  "input":{"file_path":"…/work/allowed.txt","content":"yes"},
  "description":"allowed.txt",
  "permission_suggestions":[{"type":"setMode","mode":"acceptEdits","destination":"session"}],
  "tool_use_id":"toolu_01QZ7KJFURZG1GqikT7cdVHc"}}
```

Bash requests also carry `blocked_path` and richer suggestions:
`addRules {toolName:"Bash", ruleContent:"touch bash-ok.txt"} →
localSettings`, `addDirectories → session`, and `setMode acceptEdits`.
AskUserQuestion requests carry `"requires_user_interaction": true`. **No
`title` or `decision_reason` fields appeared** in any request, so the
proposal's §5 example (`title`, `reason`) should not rely on them.

Our answers. The request id is the CLI's own UUID, echoed back:

```json
{"type":"control_response","response":{"subtype":"success","request_id":"34c79dca-…","response":{"behavior":"allow","updatedInput":{…same input…}}}}
{"type":"control_response","response":{"subtype":"success","request_id":"dbdd8a0a-…","response":{"behavior":"deny","message":"Denied by the spike: do not retry."}}}
```

- A deny becomes an `is_error` tool_result carrying our message
  (`non_execution_kind: "permission-rule"`) and is listed in
  `result.permission_denials`. The model did not retry.
- **A pending request does not time out.** A Bash request answered after
  45 s went ahead normally (`bash-1`).
- AskUserQuestion was answered structurally, with multi-select joined by
  ", " and free text for the single-select:

```json
{"behavior":"allow","updatedInput":{"questions":[…as received…],
  "answers":{"Which fruits do you like?":"apple, pear","What should I call you?":"Something else entirely: a free-text answer"}}}
```

  → tool_result `The user answered: "Which fruits do you like?"="apple, pear", …`,
  and the reply was "You like apple and pear, and want to be called something
  else entirely."

- **Not reproduced:** `pending_permission_requests` on initialize. A second
  `initialize` on the same pipe while a request was pending returned
  `success` with no such field (`bash-2`). It did report `"session_state":
  "requires_action"`, against `"idle"` otherwise. That field is presumably for
  a transport reconnecting to a live process. media-sessiond owns the pipe
  for the process's whole life, so it must keep pending requests itself. It
  had to anyway, for the approvals store.

### 5. Hooks: verified. The Stop hook speaks for a `-p` session; Meridian's silence is explained

There was one deliberately hooked run (`hooked-1`), with David's user settings
and `--include-hook-events`, and a one-sentence reply. All the hook events
fired and were reported as events:

```json
{"type":"system","subtype":"hook_started","hook_id":"…","hook_name":"SessionStart:startup","hook_event":"SessionStart"}
{"type":"system","subtype":"hook_response","hook_id":"…","hook_name":"SessionStart:startup","hook_event":"SessionStart","output":"","stdout":"","stderr":"","exit_code":0,"outcome":"success"}
```

The counts were SessionStart ×7, UserPromptSubmit ×2 and Stop ×5, all with
exit code 0. PreToolUse did not fire only because no tool ran. The first four
harness-driven scenarios show that a `can_use_tool` flow needs no hooks.

- **It spoke.** `speech-events.jsonl` has `start` at +3.7 s after the Stop
  hook fired, then `end` with `"played": true, "target": "app"`. The
  history row (id 9241) has `source_pane: ""` and `source_tmux_session: ""`,
  and `source_session` is the headless id. That matches §6.1: no pane, and
  `submit.py` keys the queue by session.
- **`TMUX_PANE`** was unset, because the harness strips `TMUX*`. The activity
  file was written:
  `~/.local/state/agent-media/activity/018ab9c1-….jsonl` → `turn`, `stop`.
- **Meridian:** it runs the SDK with `settingSources` defaulting to `[]`
  (`@rynfar/meridian` dist, `cli-*.js`: `… : pipelineCtx.settingSources ??
  []`, passed to the CLI as `--setting-sources=`). With no user settings, the
  settings.json hooks are never loaded. The spike's own hook-less runs used
  exactly that flag (`--setting-sources ""`) and left no activity file and no
  speech. Meridian transcript `2b9d0355…` has no activity file either.
  **Meridian is silent because it never loads the hooks, not because the
  hooks exit early.**
- `MEDIA_HOOK_ENABLED=0` in the child env was the second guard, and it works:
  `hook_claude_code.main` checks it before anything else. It does *not*
  silence `activity.py`, which is a separate hook.

**Library side effect:** the hooked turn was published to the Conversations
library. The manifest
`~/.local/state/agent-media/book-tracks/018ab9c1-2717-415c-a154-f11f8ba52249.json`
points at the folder
`~/conversations/p-agent-media--claude-worktrees-agent-a5b43898be9e83566-spike-headless-work/`.
Nothing in the env stops that short of not speaking. The speech-history poll
exports every conversation that spoke. **Archive or delete that item.** Note
also how it was filed: the "workspace" came from the cwd path and the title
from the worktree branch.

### 6. `prompt_suggestion`: NOT observed

In 3 runs and 9 turns (haiku and sonnet), no `prompt_suggestion` event
arrived, even though `--prompt-suggestions` was passed.

- In `-p`, the feature's init gate reports `non_interactive → disabled`
  unless `CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION` is set. That was read from the
  CLI bundle.
- With that env set, a `--debug-file` run shows the generator *running*. A
  forked `source=prompt_suggestion` API request was made 0.5–1 s after each
  `result` and took 4–6 s. Still no event was emitted, so the output was
  filtered or suppressed downstream.
- The generator also skips conversations with fewer than 2 assistant
  messages (`early_conversation`).

Conclusion: keep `ghost_prompt` for panes and the Haiku `_followup.py` as the
headless source. Treat `prompt_suggestion` as a bonus if a later version
emits it. It is not a replacement.

### 7. Transcripts and resume: verified (TUI inferred)

- The transcript is at
  `~/.claude/projects/-home-ryer-projects-agent-media--claude-worktrees-agent-a5b43898be9e83566-spike-headless-work/<id>.jsonl`.
  The slug is the cwd with `/` and `.` replaced by `-`, the usual rule.
  Entries are stamped `"entrypoint":"sdk-cli"` (Meridian's are `sdk-ts`, the
  desk's `cli`).
- A tiny session's file is about 250 KB. Most of that is a 137 KB
  `prompt_snapshot` and a 45 KB `instructions` attachment.
- `--resume <id>` from a second process, after the first exited, kept the
  **same session id**, recalled the earlier turn ("pineapple"), and read 32k
  tokens from the cache: the CLI writes the 1-hour cache
  (`ephemeral_1h_input_tokens`).
- **Headless resume of a TUI session works across directories.** `--resume
  09b971aa… --fork-session` was run from the spike cwd. That session was a
  `cli` transcript in `-home-ryer-scratch`. It answered in context, the fork
  was written under the *spike's* project dir with the old turns copied in,
  and the original file was left unchanged (30,653 bytes).
- **The "Resume from summary" modal cannot appear under `-p`.** It is an
  Ink component ("Resuming the full session will consume a substantial portion
  of your usage limits…") and `-p` renders no Ink. This is **inferred from
  code**, not measured against a cold >100k session, which would have cost a
  full reprocess to prove.
- The TUI was not opened, per the spike's rules. The file format and the
  resume-by-id path are the same ones the TUI uses, so `claude --resume <id>`
  at the desk should see it. Verify that once, by hand, at the desk.

### 8. Kill mid-turn, then resume: verified

- **SIGTERM** during a foreground tool: the CLI killed the tool (`tool_result
  "Exit code 137"`), started another request, and exited with 143 within
  0.9 s. **SIGKILL** gave rc −9, and the tool's child died with it (no orphan
  `sleep`).
- On `--resume`, nothing is replayed on stdout. The transcript gains a
  synthesized pair, `user: "Continue from where you left off."` → `assistant:
  "No response requested."`, followed by our new message. After SIGKILL the
  dangling tool_use got a synthetic `"[Request interrupted by user for tool
  use]"` result. The model then described the interruption correctly. The
  unfinished turn is **not** resumed automatically. The next user message
  simply goes ahead.

### 9. Auth: verified

`ANTHROPIC_API_KEY` (set but empty in this environment) and every `CLAUDE*`
variable were stripped. `system/init` reports `"apiKeySource": "none"`, and
the initialize response reports `"account": {"subscriptionType": "Claude
Max", "apiProvider": "firstParty"}`. So this is the OAuth subscription login.
Every stream carries a `rate_limit_event` with the 5-hour and 7-day
utilization, which the app could show. (The §9 terms question for other users
is unchanged. It is not a technical question.)

## Other envelopes worth pinning

`initialize` (optional; the CLI works without it):

```json
→ {"type":"control_request","request_id":"req_…","request":{"subtype":"initialize","hooks":null}}
← {"type":"control_response","response":{"subtype":"success","request_id":"req_…","response":{
    "commands":[…53, each {name,description,argumentHint,builtin}…],"agents":[…],"models":[…],
    "account":{…},"pid":3529656,"current_permission_mode":"default","session_state":"idle",
    "output_style":"default","fast_mode_state":"off", …}}}
```

`system/init` (fields the adapter uses):

```json
{"type":"system","subtype":"init","cwd":"…/work","session_id":"4a8631c8-…","tools":[…134…],
 "mcp_servers":[…14, claude.ai connectors still load with --setting-sources ""…],
 "model":"claude-haiku-4-5-20251001","permissionMode":"default","slash_commands":[…54…],
 "apiKeySource":"none","claude_code_version":"2.1.278",
 "capabilities":["interrupt_receipt_v1","interrupt_cancel_queued_v1","msg_lifecycle_v1"],
 "memory_paths":{"auto":"~/.claude/projects/-home-ryer-projects-agent-media/memory/"}, …}
```

Other events, in order within a turn: `system/status {"status":"requesting"}`,
then `stream_event` (`message_start`, `content_block_*`, `message_delta`,
`message_stop`, with `thinking_delta` blocks whose text is empty), then
`system/thinking_tokens`, then `assistant` (one per content block), then `user`
(tool results, with `tool_use_result`), then `system/task_started` /
`task_notification` / `task_updated` / `background_tasks_changed` for Bash
tasks, then `rate_limit_event`, and finally `result`. The `result` carries
`subtype`, `is_error`, `result`, `stop_reason`, `terminal_reason`
(`completed` | `aborted_tools` | `aborted_streaming`), `num_turns`,
`permission_denials`, `ttft_ms`, `duration_ms`, `total_cost_usd`,
`queued_turn_count` and `result_index`.

## What holds, and what to change in the proposal

Holds:

- The raw CLI from stdlib Python. Nothing needed the SDK.
- `--session-id` chosen up front.
- `can_use_tool` answered by `request_id`, and pending requests never time
  out.
- AskUserQuestion answered with `answers`, including free text.
- Interrupt with a receipt.
- Hooks under `-p`, so speech works unchanged.
- `--resume` with no modal, including a TUI session resumed from another
  directory.
- Subscription auth with the key stripped.

Change:

1. **Always send a client `uuid` on user messages** (§2/§3). It drives
   `command_lifecycle` (queued/started/completed/cancelled) and the interrupt
   receipt. Without it the receipt is empty. Add `msg_lifecycle_v1` to the
   capabilities the adapter feature-detects.
2. **Mid-turn messages merge.** A message sent while a turn runs joins that
   turn at the next tool boundary, so one `result` covers both. The contract
   should say that `/reply` during `working` is "queued, may join the current
   turn", and not promise one reply per message.
3. **Working/waiting comes from events, not sends.** A finished background
   task starts a turn on its own. Classify from `system/init`
   (turn start) → `result` (turn end), `control_request` (approval), and
   `command_lifecycle`.
4. **`system/init` is per turn, not per process,** and it is not emitted at
   spawn. For readiness at spawn, send `initialize` (1.7–2.2 s). Its response
   also carries `commands`, which serves the slash menu.
5. **`prompt_suggestion` is not usable in 2.1.278 `-p`.** Keep
   `_followup.py` as the headless source. The §3 table row and the Caps flag
   `suggestions` should say "Haiku follow-up".
6. **`pending_permission_requests` cannot be relied on.** The host keeps
   pending requests itself. `session_state: "requires_action"` from
   `initialize` is available as a cross-check.
7. **§5's approval shape:** use `display_name`, `description`, `input`,
   `permission_suggestions`, `blocked_path`, `tool_use_id` and
   `requires_user_interaction`. Drop `title`/`reason`, which were not seen.
8. **§6.4 and the marker:** the session host must run headless sessions
   *with* user settings (no `--setting-sources` override), or hooks and speech
   are gone, exactly as for Meridian. Then set `MEDIA_HEADLESS=1`. A spike or
   test harness can use `--setting-sources ""` plus `MEDIA_HOOK_ENABLED=0` to
   stay silent. Speech publishing to the library cannot be suppressed per
   session today. If test sessions should stay out of it, a
   `MEDIA_NO_LIBRARY`-style flag honoured by the history writer would be
   needed.
9. **Env hygiene for the host:** strip `TMUX*` and `CLAUDE*` (including
   `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID` and the messaging-socket vars
   inherited from a parent Claude) as well as `ANTHROPIC_API_KEY`. Otherwise
   a headless child started from inside a pane would register itself against
   that pane.
10. **Close grace:** exit after stdin closes took up to 7.2 s. Use at least
    10 s before SIGTERM (§4 says "a grace period"). SIGTERM itself is clean
    (0.9 s, child tools killed).
11. **Memory:** 100–220 MB per idle process. `MEDIA_SESSIOND_MAX=4` stands.
12. **Settings still load partly under `--setting-sources ""`.** claude.ai MCP
    connectors (14) and the project auto-memory still load. Only settings.json
    (hooks, allow rules, model, `defaultMode`) and CLAUDE.md are dropped. The
    user `allow` list (`Bash(*)`, `Write(*)`) would pre-approve almost
    everything in a hooked headless session. So phone approvals only mean
    something for tools outside those rules, or under an explicit
    `--permission-mode default` plus a narrower allow list. Decide this in the
    §5 review.
