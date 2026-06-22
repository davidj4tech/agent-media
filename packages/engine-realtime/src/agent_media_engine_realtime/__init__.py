"""OpenAI Realtime (WebSocket) TTS render engine for agent-media.

Registered under the `agent_media.render_engines` entry-point group as
`realtime`. The WebSocket flow needs the `websockets` package, which the
caller's interpreter usually lacks, so it runs in a subprocess against
`MEDIA_REALTIME_PYTHON` (a venv that has it). Config from the environment:

  MEDIA_RENDER_VOICE_REALTIME   default voice (default: marin)
  MEDIA_REALTIME_MODEL          model (default: gpt-realtime)
  MEDIA_REALTIME_PYTHON         interpreter with `websockets`
                                (falls back to CLAUDE_TTS_REALTIME_PYTHON,
                                 then this process's interpreter)
  OPENAI_API_KEY                read by the subprocess
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_VOICE = "marin"
DEFAULT_MODEL = "gpt-realtime"


def render(text: str, outfile: Path, *, voice: str | None = None) -> tuple[bool, str]:
    """Render `text` to `outfile` via the OpenAI Realtime API. Returns (ok, err)."""
    voice = voice or os.environ.get("MEDIA_RENDER_VOICE_REALTIME") or DEFAULT_VOICE
    model = os.environ.get("MEDIA_REALTIME_MODEL") or DEFAULT_MODEL
    python_bin = (os.environ.get("MEDIA_REALTIME_PYTHON")
                  or os.environ.get("CLAUDE_TTS_REALTIME_PYTHON")
                  or sys.executable)

    script_path = Path(__file__).with_name("_realtime_subprocess.py")
    cfg = json.dumps({"text": text, "model": model, "voice": voice,
                      "outfile": str(outfile)})
    proc = subprocess.run(
        [python_bin, str(script_path)],
        input=cfg.encode(),
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    err = proc.stderr.decode(errors="replace").strip()
    ok = proc.returncode == 0 and outfile.exists() and outfile.stat().st_size > 0
    return ok, err
