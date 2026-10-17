# Gemini CLI as a Sasonica agent

David, 17 Oct 2026: "let's add Gemini to sasonica settings" — then, asked how
far: **a full agent**, alongside Claude, Codex, pi, Hermes and opencode.

Already done: Gemini CLI 0.64.0 is installed on red5 (not signed in), and the
profile's `gemini` row adds a BeforeAgent `media-hook-context` hook, so a
Gemini session is told how full its context is (agent-media `e53eae2`).

The checklist below comes from the commits that added opencode (agent-media
6e6b90c, 4b97796, 520a57f, 9b866f7, ae51bfb, 8f3b66a, 86d1842, afd85b7;
sasonica-app 6ee6ad6, 55d283b, ceeb984, e889119, da28696).

## What Gemini 0.64 does (read from its bundle and docs)

- **Session ids are uuids** — `SESSION_ID` and `sessions._SESSION` already
  take them. `--session-id <uuid>` starts one with a chosen id (as pi);
  `--resume <uuid|latest|index>` resumes.
- **Transcripts**: `~/.gemini/tmp/<slug>/chats/session-<YYYY-MM-DDTHH-MM>-<id[:8]>.jsonl`.
  First line is metadata `{sessionId, projectHash, startTime, lastUpdated,
  kind, directories, summary}`; then message records keyed by `id`, a later
  line with the same id replacing the earlier, plus `{"$rewindTo": id}` and
  `{"$set": …}` patches. Subagents go under `chats/<parentId>/`, so scan
  `chats/session-*` only. `<slug>` is the project folder's basename
  (`-1`, `-2` on collision), recorded in `~/.gemini/projects.json`; each
  `tmp/<slug>/.project_root` holds the absolute path.
- **Hooks** (`~/.gemini/settings.json`, timeouts in ms, run through a shell,
  `GEMINI_SESSION_ID`/`GEMINI_CWD` in the env): AfterAgent carries `prompt`
  and `prompt_response`; BeforeTool `tool_name`/`tool_input`; SessionStart
  `source`. Context goes in as `hookSpecificOutput.additionalContext`.
- **Headless**: `-p -o stream-json` (init, message, tool_use, tool_result,
  result events), `--approval-mode default|auto_edit|yolo|plan`, `-m`.
  `--acp` speaks JSON-RPC on stdio: newSession, loadSession, prompt, cancel,
  setSessionMode, unstable_setSessionModel.
- **Sign-in**: Google OAuth cached in `oauth_creds.json` (account in
  `google_accounts.json`, method in `settings.json` `security.auth.selectedType`),
  or `GEMINI_API_KEY` / `~/.gemini/.env`. With `NO_BROWSER=true` it prints a
  URL (redirect `codeassist.google.com/authcode` shows a code) and asks for
  the code — the same shape as Codex's device sign-in, so it works from the
  phone.
- **Process**: node running `bundle/gemini.js`, which relaunches itself as a
  child (`GEMINI_CLI_NO_RELAUNCH`); the pane shows `node`.
  `GEMINI_CLI_HOME` moves its home (profiles).

## Phase 1 — Settings row, speech, threads

1. `core/harnesses.py`: `GEMINI` in `HARNESSES`; `_gemini_dir()` (honours
   `GEMINI_CLI_HOME`); `_gemini_stored()` scanning `tmp/*/chats/session-*.jsonl`
   with the full id from line one, cached by path; `_transcript()` by
   `*-{id[:8]}.jsonl` confirmed by line one; `cwd_of()` from `.project_root`;
   `title_of()` from `summary`, else the first prompt. `RECIPES[GEMINI]`:
   npm install/update, `package="@google/gemini-cli"`, sign-in is `gemini`
   itself with `NO_BROWSER=true` and `login_keys=("/auth",)` (needs a
   `Recipe.env`). `auth_state()` from files, no subprocess. `_gemini_pid()`
   for the relaunched child.
2. New `intake/hook_gemini.py` + `media-hook-gemini` + `Source.GEMINI`:
   SessionStart → `register_pane` (walk up from `getppid`); BeforeAgent →
   UserPromptSubmit (and the context note); BeforeTool → PreToolUse;
   AfterAgent → speak `prompt_response`. Through `agent_events.handle`.
3. `setup.py` `_row_gemini`: all four hooks. `activity._ALIASES` for
   Gemini's tool names (`run_shell_command`, `read_file`, `write_file`,
   `replace`, `glob`, `grep_search`, `list_directory`, `web_fetch`,
   `google_web_search`, `write_todos`).
4. `server/transcript.py`: a `GeminiBuilder` that folds the file into an
   id→record map (honouring `$rewindTo`), cached by (inode, size, mtime) —
   modelled on `_opencode_page`, since records replace each other. Into
   `messages()`, `file_state()`, `last_reply`.
5. `panes.py` (`AGENT_COMMANDS`, `agent_by_argv`, `classify`),
   `sessions.transcript_cwd`, `seal.py`, `docs/server-contract.md`.
6. App: `'gemini'` in `Harness`/`Agent` (`app/api/types.ts`), labels in
   `threadFilter.ts`, `home.tsx`, `harnesses.tsx`; mock and tests.

## Phase 2 — Start from the phone, resume

`fresh_argv` → `--session-id`, `resume_argv` → `--resume`; a fixed uuid in
`send._ask_pane`; Gemini's composer marker and escapes (measured); the
`new.tsx`/`presets.tsx` agent lists; `--skip-trust` (or `trustedFolders.json`)
so a new folder does not stop on the trust prompt.

## Phase 3 — Headless

`sessiond_gemini.py`: one `gemini --acp` per chat — loadSession to resume,
cancel to interrupt, permission requests to the approval card, setSessionMode
for strict/yolo. Fallback: `gemini -p … --resume id -o stream-json` per turn,
with no approval card.

## Phase 4 — Model picker, API key, profiles

A fixed model sheet (Auto, gemini-2.5-pro/-flash, gemini-3 previews), `/model`
in a live pane; `GEMINI_API_KEY` stored in `~/.gemini/.env` (0600) from the
app; `harness_profiles`: `"gemini": "GEMINI_CLI_HOME"`.

## Open questions

- Sign-in from the phone: `/auth` is a picker driven by keys. Does a first
  run open it by itself? The API-key route avoids it.
- Free-tier quota errors need showing, as `opencode_error_text` does.
- Does Gemini's env sanitising drop `TMUX_PANE`? Does AfterAgent fire under
  `-p`/ACP, and on a cancelled turn?
- The 8-char prefix can collide (check line one); the slug can't be derived
  from the path (use `projects.json`/`.project_root`); 30-day retention
  deletes old threads.
- A phone-started chat may have no file until its first message; does ACP
  write to the same chats folder?
- Not measured yet (needs a signed-in Gemini): pane state strings, composer
  glyph.
