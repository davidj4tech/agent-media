"""Offline Piper TTS render engine for agent-media.

Registered under the `agent_media.render_engines` entry-point group as
`piper`. It POSTs text to Piper's own HTTP server (`python -m
piper.http_server`, run as a separate process like kokoro-tts) and writes the
returned WAV to `outfile`. Piper needs no network once a voice is on disk, no
API key and no GPU, so a server on the same host is the engine that still
speaks when the tailnet and the cloud engines are down. On any failure it
returns (False, err) and core falls back.

Piper is GPL-3.0. This module talks to it over HTTP and never imports it, which
keeps this package (and core) out of the GPL. Don't add a `piper` import here.

The server loads other voices from its data dir on request, but silently uses
its default voice for one it doesn't have. So the first time a non-default
voice is asked for, check `/voices` and ask the server to `/download` it
(unless MEDIA_PIPER_DOWNLOAD=0).

Config from the environment:

  MEDIA_PIPER_BASE_URL      server base URL      (default http://127.0.0.1:5000)
  MEDIA_RENDER_VOICE_PIPER  voice name           (default: the server's own voice)
  MEDIA_PIPER_DOWNLOAD      fetch missing voices (default 1)
  MEDIA_PIPER_SPEED         speed factor         (default 1.0; >1 is faster)
  MEDIA_PIPER_SPEAKER       speaker id, for multi-speaker voices
  MEDIA_PIPER_TIMEOUT_S     request timeout      (default 30)
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1:5000"

# Voices this process has confirmed the server holds, so /voices is asked once.
_known_voices: set[str] = set()


def _post(url: str, body: dict, timeout: float) -> bytes:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _ensure_voice(base_url: str, voice: str, timeout: float) -> str:
    """Make sure the server holds `voice`. Returns "" or why it couldn't."""
    if voice in _known_voices:
        return ""
    try:
        with urllib.request.urlopen(f"{base_url}/voices", timeout=timeout) as resp:
            have = json.load(resp)
    except Exception as e:  # noqa: BLE001
        return f"piper /voices: {e}"
    if voice not in have:
        if os.environ.get("MEDIA_PIPER_DOWNLOAD", "1") == "0":
            return f"piper server has no voice {voice!r} (MEDIA_PIPER_DOWNLOAD=0)"
        try:
            # A medium voice is ~60 MB; give the download room.
            _post(f"{base_url}/download", {"voice": voice}, max(timeout, 300))
        except Exception as e:  # noqa: BLE001 — bad name, server offline from HF
            return f"piper download of {voice!r}: {e}"
    _known_voices.add(voice)
    return ""


def render(text: str, outfile: Path, *, voice: str | None = None) -> tuple[bool, str]:
    """Render `text` to a WAV at `outfile` via a Piper HTTP server."""
    base_url = (os.environ.get("MEDIA_PIPER_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    voice = voice or os.environ.get("MEDIA_RENDER_VOICE_PIPER") or ""
    try:
        timeout = float(os.environ.get("MEDIA_PIPER_TIMEOUT_S", "30"))
    except ValueError:
        timeout = 30.0

    body: dict = {"text": text}
    if voice:
        err = _ensure_voice(base_url, voice, timeout)
        if err:
            return False, err
        body["voice"] = voice
    try:
        speed = float(os.environ.get("MEDIA_PIPER_SPEED", "1.0"))
        if speed > 0 and speed != 1.0:
            # Piper's knob is phoneme duration, the inverse of speed.
            body["length_scale"] = 1.0 / speed
    except ValueError:
        pass
    speaker = os.environ.get("MEDIA_PIPER_SPEAKER", "").strip()
    if speaker.isdigit():
        body["speaker_id"] = int(speaker)

    try:
        audio = _post(f"{base_url}/synthesize", body, timeout)
    except Exception as e:  # noqa: BLE001 — any transport/server error → fall back
        return False, f"piper http: {e}"
    if len(audio) <= 44:  # nothing past a bare WAV header
        return False, "piper returned empty audio"
    try:
        outfile.write_bytes(audio)
    except OSError as e:
        return False, f"piper write failed: {e}"
    return True, ""
