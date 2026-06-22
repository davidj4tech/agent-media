# agent-media-engine-openai

OpenAI TTS render engine for [agent-media](https://github.com/davidj4tech/agent-media).
Extracted from core so the base install stays zero-config (edge-only).

```bash
pip install agent-media-engine-openai
MEDIA_RENDER_ENGINE=openai media say "hello"
```

Config (environment):

| var | default | meaning |
|---|---|---|
| `OPENAI_API_KEY` | — | required (read by the render subprocess) |
| `MEDIA_RENDER_VOICE_OPENAI` | `marin` | voice |
| `MEDIA_OPENAI_TTS_MODEL` | `gpt-4o-mini-tts` | model |
| `MEDIA_OPENAI_PYTHON` | auto-discovered | interpreter with `openai` installed |

The engine shells out to `MEDIA_OPENAI_PYTHON` (or an auto-discovered pipx
`openai`/`llm` venv, else `python3`) so this package itself doesn't depend on
the `openai` library. See the core repo's `docs/EXTENSIONS.md` for the contract.
