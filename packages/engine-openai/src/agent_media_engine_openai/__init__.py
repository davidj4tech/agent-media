"""OpenAI TTS render engine for agent-media.

Registered under the `agent_media.render_engines` entry-point group as
`openai`. Behaviour is preserved from core's original built-in: it shells out
to a Python interpreter that has the `openai` package and streams speech to a
file. Config comes from the environment:

  MEDIA_RENDER_VOICE_OPENAI   default voice (default: marin)
  MEDIA_OPENAI_TTS_MODEL      model (default: gpt-4o-mini-tts)
  MEDIA_OPENAI_PYTHON         interpreter with `openai` installed
                              (default: auto-discover a pipx venv, else python3)
  OPENAI_API_KEY              read by the subprocess
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

DEFAULT_VOICE = "marin"
DEFAULT_MODEL = "gpt-4o-mini-tts"


def default_openai_python(current: str) -> str:
    """If `current` lacks the `openai` module, look for a pipx venv that has
    it. Returns `current` unchanged if nothing better is found."""
    pipx_root = Path(os.environ.get("PIPX_HOME", Path.home() / ".local" / "pipx"))
    for c in (current,
              str(pipx_root / "venvs" / "openai" / "bin" / "python3"),
              str(pipx_root / "venvs" / "llm" / "bin" / "python3")):
        if not c:
            continue
        try:
            r = subprocess.run([c, "-c", "import openai"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=2)
            if r.returncode == 0:
                return c
        except (OSError, subprocess.SubprocessError):
            continue
    return current


def render(text: str, outfile: Path, *, voice: str | None = None) -> tuple[bool, str]:
    """Render `text` to `outfile` via OpenAI TTS. Returns (ok, err)."""
    voice = voice or os.environ.get("MEDIA_RENDER_VOICE_OPENAI") or DEFAULT_VOICE
    model = os.environ.get("MEDIA_OPENAI_TTS_MODEL") or DEFAULT_MODEL
    python_bin = os.environ.get("MEDIA_OPENAI_PYTHON") or default_openai_python("python3")

    script = (
        "import os\n"
        "from openai import OpenAI\n"
        "client = OpenAI()\n"
        "with client.audio.speech.with_streaming_response.create(\n"
        "    model=os.environ['TTS_MODEL'],\n"
        "    voice=os.environ['TTS_VOICE'],\n"
        "    input=os.environ['TTS_TEXT'],\n"
        ") as r:\n"
        "    r.stream_to_file(os.environ['TTS_OUTFILE'])\n"
    )
    env = {**os.environ, "TTS_MODEL": model, "TTS_VOICE": voice,
           "TTS_TEXT": text, "TTS_OUTFILE": str(outfile)}
    proc = subprocess.run(
        [python_bin, "-c", script],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    err = proc.stderr.decode(errors="replace").strip()
    ok = proc.returncode == 0 and outfile.exists() and outfile.stat().st_size > 0
    return ok, err
