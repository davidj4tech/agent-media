# agent-media-intake-codex

Codex (OpenAI CLI) intake hook for
[agent-media](https://github.com/davidj4tech/agent-media): speaks Codex turn
output via the stdin-pipe hook convention. Extracted from core as an optional,
separately-installable intake source.

```bash
pip install agent-media-intake-codex   # pulls in agent-media-core
# wire `media-hook-codex` into Codex's notify bridge
```

See the core repo's `docs/reference/extensions.md` (§2 Intake adapters).
