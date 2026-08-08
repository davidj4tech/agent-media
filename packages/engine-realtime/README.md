# agent-media-engine-realtime

OpenAI Realtime (WebSocket) TTS render engine for
[agent-media](https://github.com/davidj4tech/agent-media). Extracted from core
so the base install stays zero-config (edge-only).

The WebSocket flow needs the `websockets` package. Rather than depend on it
directly, the engine runs the flow in a subprocess against a Python interpreter
you point it at — usually a small venv that has `websockets`.

```bash
python -m venv ~/.local/share/realtime-venv && ~/.local/share/realtime-venv/bin/pip install websockets
pip install agent-media-engine-realtime
export OPENAI_API_KEY=... MEDIA_REALTIME_PYTHON=~/.local/share/realtime-venv/bin/python
MEDIA_RENDER_ENGINE=realtime media say "hello"
```

Config (environment):

| var | default | meaning |
|---|---|---|
| `OPENAI_API_KEY` | — | required (read by the subprocess) |
| `MEDIA_REALTIME_PYTHON` | this interpreter | interpreter with `websockets` |
| `MEDIA_RENDER_VOICE_REALTIME` | `marin` | voice |
| `MEDIA_REALTIME_MODEL` | `gpt-realtime` | model |

See the core repo's `docs/reference/extensions.md` for the contract.
