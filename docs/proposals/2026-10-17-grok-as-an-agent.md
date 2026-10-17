# Grok CLI as a Sasonica agent

David, 17 Oct 2026, through agent-mail #110 (from the Paseo-trial session):
add xAI's Grok CLI ("Grok Build") as the seventh coding agent, the way Gemini
CLI was added the same day (`2026-10-17-gemini-as-an-agent.md`).

Already done: Grok 1.0.62 is installed on red5 in `~/.grok` (binary
`~/.grok/bin/grok`, linked as `~/.local/bin/grok`), signed in by device code
as davidj4@gmail.com (the subscriptions account; API keys belong to
davidj4tech@gmail.com). Grok imported Claude Code's hooks from
`~/.claude/settings.json` and ran them in the foreground — it ignores
`async`, so the 120 s speech Stop hook held every turn, and agent-mail,
catch-up and rtk fired inside Grok. Turned off on red5 (17 Oct 2026) with
`[compat.claude] hooks = false` in `~/.grok/config.toml`; the profile row
below sets it on any machine. Claude's skills, rules, CLAUDE.md and MCP
servers stay imported (their own compat cells). Grok also always reads
Claude's `permissions`; no cell turns that off.

## What Grok 1.0.62 does (its shipped docs, `grok inspect`, the binary)

- **Settings**: `~/.grok/config.toml`. `GROK_HOME` moves the whole home
  (profiles). Compat cells `[compat.claude]` `hooks|skills|rules|agents|mcps`,
  env override `GROK_CLAUDE_HOOKS_ENABLED`.
- **Hooks**: any number of `~/.grok/hooks/*.json`, Claude-shaped
  (`{"hooks":{"<Event>":[{"matcher","hooks":[{"type":"command","command","timeout"}]}]}}`),
  timeout in **seconds**, no async (a hook backgrounds itself). Global hooks
  are trusted. Events: SessionStart, UserPromptSubmit, PreToolUse,
  PostToolUse, PostToolUseFailure, PermissionDenied, Stop, StopFailure,
  StopCancelled, Notification, SubagentStart/Stop, Pre/PostCompact,
  SessionEnd. Stdin: `hookEventName` (snake value), `hook_event_name`
  (Pascal), `sessionId`, `cwd`, `workspaceRoot`, `timestamp`,
  `permissionMode`, `promptId`, `transcriptPath` (+ snake_case aliases);
  tool events add `toolName`, `toolInput`, `toolUseId`; PostToolUse
  `toolResult`. **Stop carries the reply** in `lastAssistantMessage` (≤32 KB)
  and `reason`; a second Stop fires at session end, so only
  `reason == "end_turn"` is a reply. Env: `GROK_SESSION_ID`,
  `GROK_HOOK_EVENT`, `GROK_WORKSPACE_ROOT`.
- **Context for the model** only from PreToolUse/PostToolUse/
  PostToolUseFailure `additionalContext` — UserPromptSubmit and SessionStart
  can't add any. The context warning goes on PostToolUse.
- **Sessions**: `~/.grok/sessions/<url-encoded-cwd>/<uuidv7>/` (a `.cwd` file
  when the name is too long) with `summary.json` (`info{id,cwd}`,
  `generated_title`, `session_summary`, `created_at`, `updated_at`,
  `current_model_id`, `num_messages`, `last_turn_summary`, `last_recap`,
  `parent_session_id`), `updates.jsonl` (ACP `session/update` events — the
  authoritative log), `chat_history.jsonl`, `signals.json` (token usage).
  Search index `sessions/session_search.sqlite`. No session existed on disk
  when this was written, so the record shapes need confirming on a real one.
- **CLI**: `-s/--session-id <uuid>` (new only), `-r/--resume <id>`, `-c`,
  `-p` headless with `--output-format json|streaming-json|…`,
  `--always-approve`, `--permission-mode`, `-m`. `grok agent stdio` is ACP.
  A static Rust binary (`~/.grok/downloads/grok-linux-x86_64`).
- **Auth**: `~/.grok/auth.json` (0600; `key`, `refresh_token`, `expires_at`,
  `auth_mode`, `email`). `grok login --device-auth` prints a URL and a code —
  works from the phone. `grok logout`. `XAI_API_KEY` only as a fallback.
- **Install/update**: `curl -fsSL https://x.ai/cli/install.sh | bash`;
  `grok update`; `grok update --check --json` →
  `{currentVersion, latestVersion, updateAvailable}`.
- **Context**: grok-4.7 has a 256k window (`~/.grok/models_cache.json`
  `context_window`); auto-compacts at 80%
  (`model.<id>.auto_compact_threshold_percent`).

## Phase 1 — Settings row, speech, threads (as Gemini's phase 1)

1. `core/harnesses.py`: `GROK` in `HARNESSES`; `_grok_dir()` (`GROK_HOME`);
   stored sessions from `sessions/*/*/summary.json`; transcript, cwd and
   title from it; live process by the binary's path. `RECIPES[GROK]`: install
   via the curl script, `update=("grok","update")`,
   `check=("update","--check","--json")`, `login=("login","--device-auth")`,
   `logout=("logout",)`; `auth_state` from `auth.json`.
2. `intake/hook_grok.py` + `media-hook-grok` + `Source.GROK`: SessionStart →
   register_pane; UserPromptSubmit → the "You:" turn; PreToolUse → a step;
   Stop with `reason == "end_turn"` → speak `lastAssistantMessage`, the hook
   backgrounding its own work so the turn is not held; PostToolUse → the
   context note as `additionalContext` (counted against 80% of the window,
   where Grok compacts). Through `agent_events.handle`.
3. `setup.py` `_row_grok`: `[compat.claude] hooks = false` in
   `~/.grok/config.toml` (left alone if set either way by hand? — no: on is
   the hazard, so the row turns it off and says so) and
   `~/.grok/hooks/agent-media.json` with the hooks above.
4. `server/transcript.py`: a Grok reader over `updates.jsonl` (or
   `chat_history.jsonl`, whichever carries whole messages), cached like
   Gemini's.
5. `panes.py`, `sessions.transcript_cwd`, `seal.py`, `reap.py`,
   `docs/server-contract.md`.
6. App: `'grok'` in `Harness`/`Agent`, label "Grok" everywhere Gemini's was
   added.

## Later — as Gemini's phases 2–4

Start from the phone (`-s <uuid>`, resume `-r`), headless over `grok agent
stdio` (ACP, like Gemini's `--acp`), model sheet, `XAI_API_KEY` (from the
davidj4tech account), profiles via `GROK_HOME`.
