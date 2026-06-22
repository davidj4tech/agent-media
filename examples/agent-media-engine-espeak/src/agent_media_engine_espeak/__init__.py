"""Example agent-media render engine: offline TTS via espeak-ng.

This is a complete, working reference for the `agent_media.render_engines`
extension contract. Copy it as a starting point for a real engine (Piper,
Coqui, a cloud vendor, …).

The whole contract is one function with this signature:

    render(text: str, outfile: Path, *, voice: str | None = None) -> (ok, err)

- Write a WAV/MP3 to `outfile` and return (True, "").
- On failure return (False, "<why>") — core logs it and falls back to edge.
- Read any config (model, API key, base URL) from os.environ yourself; core
  passes none of it, so the engine stays self-describing.

Register it in your pyproject under:

    [project.entry-points."agent_media.render_engines"]
    espeak = "agent_media_engine_espeak:render"

Then `pip install` your package alongside agent-media-core and select it with
`MEDIA_RENDER_ENGINE=espeak` (or `render_text(..., engine="espeak")`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# espeak voice; override per-engine config via env (the convention is
# MEDIA_RENDER_VOICE_<ENGINE>, but engines may read whatever they like).
DEFAULT_VOICE = os.environ.get("MEDIA_RENDER_VOICE_ESPEAK", "en")


def render(text: str, outfile: Path, *, voice: str | None = None) -> tuple[bool, str]:
    """Render `text` to a WAV at `outfile` using espeak-ng. Returns (ok, err)."""
    binary = shutil.which("espeak-ng") or shutil.which("espeak")
    if not binary:
        return False, "espeak-ng/espeak not found on PATH"
    cmd = [binary, "-v", voice or DEFAULT_VOICE, "-w", str(outfile), text]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"espeak invocation failed: {e}"
    if proc.returncode != 0:
        return False, f"espeak exited {proc.returncode}: {proc.stderr.strip()}"
    if not outfile.exists() or outfile.stat().st_size == 0:
        return False, "espeak produced no audio"
    return True, ""
