"""Optional LLM 'spoken summary' rewrite for the Claude Code Stop hook.

When ``MEDIA_SPEECH_SUMMARY=1``, a long assistant reply is rewritten into a
short, speech-friendly paraphrase before TTS — describing code / tables /
commands in a phrase instead of reading them and dropping URLs and paths —
rather than the mechanical markdown-strip.

It runs in the *detached* playback child (never on the hook's hot path), and
shells out to an ``openai``-capable interpreter exactly like the OpenAI render
engine, so it reuses ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` and can be pointed
at a local OpenAI-compatible gateway (e.g. a LiteLLM proxy + local model).

Every failure mode — disabled, too short, no interpreter, API error, timeout,
empty output — returns ``None`` so the caller keeps the mechanically-stripped
text. It never raises.

Config (env / ~/.config/agent-media.env):
  MEDIA_SPEECH_SUMMARY     "1" to enable (default off)
  MEDIA_SUMMARY_MODEL      chat model (default gpt-4o-mini)
  MEDIA_SUMMARY_MIN_CHARS  only summarize replies at least this long (default 320)
  MEDIA_SUMMARY_TIMEOUT    subprocess timeout seconds (default 20)
  MEDIA_SUMMARY_PYTHON     interpreter with `openai` (falls back to
                           MEDIA_OPENAI_PYTHON, then a pipx venv, then python3)
  MEDIA_SUMMARY_PROMPT     override the system prompt
  OPENAI_API_KEY / OPENAI_BASE_URL   read by the subprocess
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_MIN_CHARS = 320
DEFAULT_TIMEOUT = 20

DEFAULT_PROMPT = (
    "You turn an assistant's chat reply into a short spoken summary for "
    "text-to-speech. Rewrite it as plain spoken prose: 1-3 sentences, no "
    "markdown, no bullet points, no code, no URLs or file paths. Describe any "
    "code, commands, or tables in a brief phrase instead of quoting them. Keep "
    "the key result or answer. Output only the spoken text, nothing else."
)


def summary_enabled() -> bool:
    return os.environ.get("MEDIA_SPEECH_SUMMARY", "0") == "1"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def summary_min_chars() -> int:
    return _int_env("MEDIA_SUMMARY_MIN_CHARS", DEFAULT_MIN_CHARS)


def _summary_python() -> str:
    """An interpreter that can `import openai`: explicit override, then the
    OpenAI engine's interpreter, then a pipx `openai`/`llm` venv, then python3."""
    explicit = (os.environ.get("MEDIA_SUMMARY_PYTHON")
                or os.environ.get("MEDIA_OPENAI_PYTHON"))
    if explicit:
        return explicit
    pipx_root = Path(os.environ.get("PIPX_HOME", Path.home() / ".local" / "pipx"))
    for c in ("python3",
              str(pipx_root / "venvs" / "openai" / "bin" / "python3"),
              str(pipx_root / "venvs" / "llm" / "bin" / "python3")):
        try:
            r = subprocess.run([c, "-c", "import openai"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=3)
            if r.returncode == 0:
                return c
        except (OSError, subprocess.SubprocessError):
            continue
    return "python3"


_SCRIPT = (
    "import os, sys\n"
    "from openai import OpenAI\n"
    "client = OpenAI()\n"
    "r = client.chat.completions.create(\n"
    "    model=os.environ['SUM_MODEL'],\n"
    "    messages=[{'role': 'system', 'content': os.environ['SUM_SYS']},\n"
    "              {'role': 'user', 'content': os.environ['SUM_TEXT']}],\n"
    "    temperature=0.3,\n"
    ")\n"
    "sys.stdout.write((r.choices[0].message.content or '').strip())\n"
)


def summarize_for_speech(text: str) -> str | None:
    """Rewrite `text` into a short spoken summary via an OpenAI-compatible chat
    model. Returns the summary, or ``None`` on any problem (caller falls back)."""
    text = (text or "").strip()
    if not text:
        return None
    model = os.environ.get("MEDIA_SUMMARY_MODEL") or DEFAULT_MODEL
    prompt = os.environ.get("MEDIA_SUMMARY_PROMPT") or DEFAULT_PROMPT
    timeout = _int_env("MEDIA_SUMMARY_TIMEOUT", DEFAULT_TIMEOUT)
    env = {**os.environ, "SUM_MODEL": model, "SUM_SYS": prompt, "SUM_TEXT": text}
    try:
        proc = subprocess.run(
            [_summary_python(), "-c", _SCRIPT],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode(errors="replace").strip()
    return out or None
