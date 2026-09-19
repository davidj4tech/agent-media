# agent-media-intake-codex

Codex (OpenAI CLI) intake hook for
[agent-media](https://github.com/davidj4tech/agent-media): speaks Codex turn
output via the stdin-pipe hook convention. Extracted from core as an optional,
separately-installable intake source.

```bash
pip install agent-media-intake-codex   # pulls in agent-media-core
```

Set a top-level entry in `~/.codex/config.toml` (use the absolute executable
path if it is installed in a virtual environment), then restart Codex:

```toml
notify = ["media-hook-codex"]
```

Codex passes a JSON argument. The adapter speaks only `agent-turn-complete`
events, extracts `last-assistant-message`, and preserves `thread-id` as
`session`, `turn-id` as `turn_id`, and `cwd` in speech-event metadata.
Other event types and empty replies are ignored; malformed JSON returns an
error without speaking its contents. With no arguments, plain-text stdin is
still supported. Both paths use the shared speech pipeline and `CODEX_TTS_*`
settings.

A reply that is a bare JSON object is not spoken: Codex names each thread
with a hidden turn that answers `{"title": "…"}`.

## Prompts and steps (hooks)

For the phone's transcript to show what you typed, and its step list what
Codex is doing, add these to `~/.codex/hooks.json` (the same events Claude
Code's hooks send):

```json
{"hooks": {
  "UserPromptSubmit": [{"hooks": [{"type": "command", "timeout": 10,
    "command": "/path/to/.venv/bin/media-hook-codex event"}]}],
  "PreToolUse": [{"hooks": [{"type": "command", "timeout": 5,
    "command": "python3 -I /path/to/agent_media_core/activity.py"}]}],
  "Stop": [{"hooks": [{"type": "command", "timeout": 5,
    "command": "python3 -I /path/to/agent_media_core/activity.py"}]}]
}}
```

Codex asks once, at the desk, to trust new or changed hooks ("Hooks need
review"). Until someone answers, a session the phone opens waits on that
prompt and nothing is typed into it.

See the core repo's `docs/reference/extensions.md` (§2 Intake adapters).
