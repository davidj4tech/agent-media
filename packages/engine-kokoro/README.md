# agent-media-engine-kokoro

Local [Kokoro](https://github.com/thewh1teagle/kokoro-onnx) TTS render engine
for agent-media. POSTs text to a small kokoro-onnx HTTP server (the `kokoro-tts`
service, normally on red5) and writes back WAV — all on the tailnet, no cloud,
no API key. On any failure it returns `(False, err)` so core falls back to edge.

## Use

```sh
pip install -e packages/engine-kokoro
export MEDIA_RENDER_ENGINE=kokoro
export MEDIA_KOKORO_BASE_URL=http://red5:8880   # red5
```

Config (env): `MEDIA_KOKORO_BASE_URL`, `MEDIA_RENDER_VOICE_KOKORO` (default
`af_heart`), `MEDIA_KOKORO_LANG` (default `en-us`), `MEDIA_KOKORO_SPEED`,
`MEDIA_KOKORO_TIMEOUT_S`.

## Server

The matching server is `server.py` on red5 (`~/kokoro-tts/server.py`, run by the
`kokoro-tts` systemd user service). It loads the kokoro-onnx model once and
serves `POST /tts` (`{"text","voice"?,"speed"?,"lang"?}` → `audio/wav`) and
`GET /health`, bound to the tailscale IP.
