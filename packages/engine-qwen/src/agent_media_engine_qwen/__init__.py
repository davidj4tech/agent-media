"""Qwen / DashScope TTS render engine for agent-media.

Registered under the `agent_media.render_engines` entry-point group as `qwen`.
Behaviour is preserved from core's original built-in. Config from the
environment:

  MEDIA_RENDER_VOICE_QWEN   default voice (default: Cherry)
  MEDIA_QWEN_MODEL          model (default: qwen3-tts-flash-2025-11-27)
  MEDIA_QWEN_LANG           language (default: English)
  MEDIA_QWEN_BASE_URL       API base (default: dashscope-intl …/api/v1)
  DASHSCOPE_API_KEY         required
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

DEFAULT_VOICE = "Cherry"
DEFAULT_MODEL = "qwen3-tts-flash-2025-11-27"
DEFAULT_LANG = "English"
DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/api/v1"


def render(text: str, outfile: Path, *, voice: str | None = None) -> tuple[bool, str]:
    """Render `text` to `outfile` via DashScope. Returns (ok, err)."""
    voice = voice or os.environ.get("MEDIA_RENDER_VOICE_QWEN") or DEFAULT_VOICE
    model = os.environ.get("MEDIA_QWEN_MODEL") or DEFAULT_MODEL
    language = os.environ.get("MEDIA_QWEN_LANG") or DEFAULT_LANG
    base_url = os.environ.get("MEDIA_QWEN_BASE_URL") or DEFAULT_BASE_URL
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        return False, "DASHSCOPE_API_KEY not set"

    url = f"{base_url.rstrip('/')}/services/aigc/multimodal-generation/generation"
    payload = json.dumps({
        "model": model,
        "input": {"text": text, "voice": voice, "language_type": language},
    }).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    except Exception as e:  # noqa: BLE001
        return False, f"qwen http: {e}"
    try:
        data = json.loads(body)
    except ValueError as e:
        return False, f"qwen json: {e}"
    audio_url = (
        (data.get("output") or {}).get("audio", {}).get("url")
        or (data.get("output") or {}).get("url")
        or data.get("url")
    )
    if not audio_url:
        return False, f"qwen response missing audio url: {json.dumps(data)[:200]}"
    try:
        with urllib.request.urlopen(audio_url, timeout=60) as resp:
            outfile.write_bytes(resp.read())
    except Exception as e:  # noqa: BLE001
        return False, f"qwen download: {e}"
    if not outfile.exists() or outfile.stat().st_size == 0:
        return False, "qwen download produced empty file"
    return True, ""
