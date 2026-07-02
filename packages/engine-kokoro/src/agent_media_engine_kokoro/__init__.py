"""Local Kokoro TTS render engine for agent-media.

Registered under the `agent_media.render_engines` entry-point group as
`kokoro`. It POSTs text to a small kokoro-onnx HTTP server (see the
`kokoro-tts` service that ships with this repo, normally on red5) and writes
the returned WAV to `outfile`. Everything stays on the tailnet — no cloud, no
API key — so it satisfies the reliability/privacy goals that the cloud engines
(edge, qwen, openai) can't. On any failure it returns (False, err) and core
falls back to edge, so a red5 hiccup never drops a sentence.

Config from the environment:

  MEDIA_KOKORO_BASE_URL     server base URL (default http://red5:8880)
  MEDIA_RENDER_VOICE_KOKORO default voice    (default af_heart)
  MEDIA_KOKORO_LANG         language         (default en-us)
  MEDIA_KOKORO_SPEED        speed factor     (default 1.0)
  MEDIA_KOKORO_TIMEOUT_S    request timeout  (default 30)
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "http://red5:8880"
DEFAULT_VOICE = "af_heart"
DEFAULT_LANG = "en-us"


def render(text: str, outfile: Path, *, voice: str | None = None) -> tuple[bool, str]:
    """Render `text` to a WAV at `outfile` via the local Kokoro server."""
    base_url = (os.environ.get("MEDIA_KOKORO_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    voice = voice or os.environ.get("MEDIA_RENDER_VOICE_KOKORO") or DEFAULT_VOICE
    lang = os.environ.get("MEDIA_KOKORO_LANG") or DEFAULT_LANG
    try:
        speed = float(os.environ.get("MEDIA_KOKORO_SPEED", "1.0"))
    except ValueError:
        speed = 1.0
    try:
        timeout = float(os.environ.get("MEDIA_KOKORO_TIMEOUT_S", "30"))
    except ValueError:
        timeout = 30.0

    payload = json.dumps(
        {"text": text, "voice": voice, "lang": lang, "speed": speed}
    ).encode()
    req = urllib.request.Request(
        f"{base_url}/tts", data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            audio = resp.read()
    except Exception as e:  # noqa: BLE001 — any transport/server error → fall back to edge
        return False, f"kokoro http: {e}"
    if not audio:
        return False, "kokoro returned empty audio"
    try:
        outfile.write_bytes(audio)
    except OSError as e:
        return False, f"kokoro write failed: {e}"
    if not outfile.exists() or outfile.stat().st_size == 0:
        return False, "kokoro produced empty file"
    return True, ""
