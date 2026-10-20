# Model and Plan buttons

The server reports a thread's settings through `/session/settings`. The
app renders `can.model` and `can.plan`; a new-chat sheet can report the same
capabilities. Older hosts retain the existing Claude, opencode and pi UI.

As of 20 October 2026:

| Harness | Model | Plan |
| --- | --- | --- |
| Claude Code | Existing pane and headless controls | Existing controls |
| Codex | Native picker, conversation only | Native collaboration mode |
| opencode | Existing model controls | Not exposed |
| pi | Existing model controls | Not exposed |
| Grok | Native `/model` command | Not exposed |
| Hermes, Gemini | Not exposed | Not exposed |

Codex and Grok list the account's visible models from `models_cache.json`.
A Codex harness profile uses its own `CODEX_HOME`. Missing or malformed
catalogues produce an empty model sheet, with no invented fallback names.
Credentials and provider configuration are excluded from responses.

Codex 0.163+ uses its existing shared daemon's Unix WebSocket and native
`thread/settings/update`. Reading a loaded thread rejoins it without
configuration overrides to read the effective collaboration mode. It never
loads an ended thread or starts a daemon. Model and plan changes leave
permissions and global defaults untouched. This also supports Codex threads
that the older rollout-file-to-terminal lookup cannot discover.

Other live changes require an idle tmux pane. A working turn, question, or missing
pane is refused. As a fallback, Codex's `/model` opens its picker; the server locates the
exact highlighted model label and uses its `s session` action, leaving the
global default alone. This follows Codex 0.163.0's native picker. Missing
rows or changed UI produce an error. The footer must confirm the model or
plan setting before success is returned. Herdr does not expose the pane
fallback; a thread on the shared Codex daemon can still use its native API.

New Codex chats use `--model`, and `/plan <first prompt>` when requested;
the native command switches mode before submitting the first prompt.
Grok uses `--model` for a new chat. Neither sends a natural-language request
asking the assistant to imitate a different mode or model.
