"""Optional LLM 'spoken summary' rewrite for the Claude Code Stop hook.

When ``MEDIA_SPEECH_SUMMARY=1``, a long assistant reply is rewritten into a
short, speech-friendly paraphrase before TTS — describing code / tables /
commands in a phrase instead of reading them and dropping URLs and paths —
rather than the mechanical markdown-strip.

It runs in the *detached* playback child (never on the hook's hot path) and
talks to any OpenAI-compatible ``/chat/completions`` endpoint over stdlib
``urllib`` — no SDK or extra interpreter needed. Point it at a local gateway
(e.g. a LiteLLM proxy + local model) via its own ``MEDIA_SUMMARY_BASE_URL`` /
``MEDIA_SUMMARY_API_KEY`` so it never collides with the OpenAI *TTS* fallback's
``OPENAI_BASE_URL`` (which must stay pointed at real OpenAI).

Every failure mode — disabled, too short, no endpoint, HTTP/JSON error,
timeout, empty output — returns ``None`` so the caller keeps the mechanically
-stripped text. It never raises.

Config (env / ~/.config/agent-media.env):
  MEDIA_SPEECH_SUMMARY     "1" to enable (default off)
  MEDIA_SUMMARY_MODEL      chat model (default gpt-4o-mini)
  MEDIA_SUMMARY_BASE_URL   OpenAI-compatible base, incl. /v1 (falls back to
                           OPENAI_BASE_URL, then https://api.openai.com/v1)
  MEDIA_SUMMARY_API_KEY    bearer token (falls back to OPENAI_API_KEY)
  MEDIA_SUMMARY_MIN_CHARS  only summarize replies at least this long (default 320)
  MEDIA_SUMMARY_TIMEOUT    request timeout seconds (default 30)
  MEDIA_SUMMARY_PROMPT     override the system prompt
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MIN_CHARS = 320
DEFAULT_TIMEOUT = 30

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


def summarize_for_speech(text: str) -> str | None:
    """Rewrite `text` into a short spoken summary via an OpenAI-compatible chat
    endpoint. Returns the summary, or ``None`` on any problem (caller falls
    back to the mechanically-stripped text). Never raises."""
    text = (text or "").strip()
    if not text:
        return None
    base = (os.environ.get("MEDIA_SUMMARY_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or DEFAULT_BASE_URL).rstrip("/")
    api_key = (os.environ.get("MEDIA_SUMMARY_API_KEY")
               or os.environ.get("OPENAI_API_KEY") or "")
    model = os.environ.get("MEDIA_SUMMARY_MODEL") or DEFAULT_MODEL
    prompt = os.environ.get("MEDIA_SUMMARY_PROMPT") or DEFAULT_PROMPT
    timeout = _int_env("MEDIA_SUMMARY_TIMEOUT", DEFAULT_TIMEOUT)

    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": prompt},
                     {"role": "user", "content": text}],
        "temperature": 0.3,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    try:
        out = (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return None
    return out or None
