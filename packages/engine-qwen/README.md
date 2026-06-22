# agent-media-engine-qwen

Qwen / DashScope TTS render engine for
[agent-media](https://github.com/davidj4tech/agent-media). Extracted from core
so the base install stays zero-config (edge-only). Stdlib-only (no extra deps).

```bash
pip install agent-media-engine-qwen
export DASHSCOPE_API_KEY=...
MEDIA_RENDER_ENGINE=qwen media say "hello"
```

Config (environment):

| var | default | meaning |
|---|---|---|
| `DASHSCOPE_API_KEY` | — | required |
| `MEDIA_RENDER_VOICE_QWEN` | `Cherry` | voice |
| `MEDIA_QWEN_MODEL` | `qwen3-tts-flash-2025-11-27` | model |
| `MEDIA_QWEN_LANG` | `English` | language |
| `MEDIA_QWEN_BASE_URL` | dashscope-intl `…/api/v1` | API base |

See the core repo's `docs/EXTENSIONS.md` for the contract.
