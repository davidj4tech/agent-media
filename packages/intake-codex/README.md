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

See the core repo's `docs/reference/extensions.md` (§2 Intake adapters).
