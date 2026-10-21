# Antigravity CLI migration (21 October 2026)

Google's replacement executable is `agy`, installed on this host in
`~/.local/bin/agy`. It is a separate harness, not an alias for `gemini`.

## Configuration

- agent-config: `bin/agent-antigravity-setup` and
  `harnesses/antigravity/install.sh` link the shared global instructions and
  skills. Its normal core installer discovers the new harness automatically.
- Instructions: `~/.gemini/config/AGENTS.md` links to agent-config/AGENTS.md.
- Skills: per-skill links under `~/.gemini/antigravity-cli/skills`, sourced
  from `~/.agents/skills` and any Gemini-only legacy skills. Existing local
  entries are preserved.
- MCP: missing legacy servers are merged into
  `~/.gemini/config/mcp_config.json`; remote `httpUrl`/`url` becomes
  `serverUrl`. Current Antigravity entries take precedence. Nothing is
  printed or committed from the live configuration. This host had no legacy
  Gemini MCP servers to migrate.
- Preferences: `~/.gemini/antigravity-cli/settings.json` is managed by
  Antigravity's `/settings`; old Gemini permissions and preferences are not
  copied into this different schema.
- agent-workspace: the `antigravity` package uses `agy update` and the shared
  setup helper. The old `gemini` installer forwards to it, preserving the old
  skip-upgrade environment variable. Neovim's Antigravity preset uses the
  existing Google keybindings; direct legacy preset lookups still work.
- dotfiles: zsh already contains the PATH additions made by the CLI installer.
  `packages/aicli/ANTIGRAVITY.md` records ownership of configuration. Gemini
  model IDs in Crush, OpenCode and Pi are unrelated to the deprecated CLI.
- Sasonica Settings offers Antigravity in place of Gemini, resolves it to
  `agy`, offers its updater and opens startup for sign-in. On a remote host
  the SSH sign-in flow prints a URL and accepts the browser's code.

## Compatibility boundaries

Legacy Gemini conversation history, its transcript reader, credentials and
hooks are retained. They must not be relabelled as Antigravity data.
Antigravity stores credentials in the OS keyring; Settings reports its auth
state as unknown rather than inferring sign-in from Gemini files. The
installed CLI has no `--version`, so the row does not invent a version.

## App conversations (21 October 2026)

Antigravity is available in New chat and the thread filter. App conversations
run through sessiond using the native persistent `--input-format stream-json`
protocol, even on a host whose other agents use panes. The adapter waits for
`init` before sending a prompt: a prompt sent before the ready event stalled
in the real resume smoke test. It does not send Claude control envelopes.

The app's UUID stays stable; Antigravity's own UUID is stored separately as
`native_session` in the session record and supplied with `--conversation` on
resume. Replies, user messages and tool activity are exported under
`$XDG_STATE_HOME/agent-media/antigravity/<app-id>.jsonl` in the common message
schema. They keep the `antigravity` harness identity and have their own
reader registration. Existing Gemini history remains untouched.

The named `agent-media-phone` hook in `~/.gemini/config/hooks.json` records
PreToolUse/PostToolUse activity and forwards approvals to sessiond's private
socket. Read-only tools defer to the CLI's existing policy; other tools get
phone permission cards, with Allow/Deny returned to the hook. Questions get
the same app cards; their answers are returned in a denial reason to the
native question tool so the model can continue without a terminal prompt.
Explicit CLI denies still apply. Hook failure denies the operation. Outside
an app-managed session, this hook leaves native policy in charge.

Successful final responses enter normal speech intake with the app session
and workspace attached. Failed/cancelled results are visible but not spoken.
Native streams lacking a result get an error from stderr in the transcript.
Speech tests capture the intake boundary; no listening checks are performed.

Later work: model/effort controls, additional Antigravity account profiles,
and importing conversations started independently in a terminal. The
vendored amux Gemini adapter is retained for its legacy sessions.

No agent-mail messages were sent, no phone was driven, no authentication
tokens were copied, and the deprecated npm CLI was not uninstalled.

## Validation

Isolated fake-agent tests cover start, follow-up, parked resume with the
native ID retained, readable transcript identity, approval/denial, question
answers, error handling, hook merging and settings capabilities. Existing
session-host, restart, transcript and model-settings regressions run alongside
them. Core harness regressions also pass. The app has a private-mock browser
check for Antigravity launch and its agent identity; typecheck and both web
bundles are built.

A real CLI smoke read a scratch file, approved a harmless printf through the
bridge, completed follow-ups, and resumed the same native conversation twice
after the ready handshake. Audio was captured rather than played, and state
was isolated from the app's real history.

## Sources

- https://antigravity.google/docs/cli/gcli-migration/
- https://antigravity.google/docs/cli/using/
- https://antigravity.google/docs/hooks/
- https://antigravity.google/docs/install?tab=cli

- https://antigravity.google/docs/cli/headless/
