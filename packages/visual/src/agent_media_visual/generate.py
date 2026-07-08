"""Reply text → image prompt → generated image (Venice API).

Two steps, both best-effort and off any hot path:

1. *Prompt shaping*: one chat call to the same OpenAI-compatible gateway the
   summary/describe path uses (MEDIA_SUMMARY_BASE_URL + LiteLLM master key —
   mirrors core's intake/_summary.py resolution) turns the spoken reply into
   a single vivid scene description. Falls back to the raw text on any
   failure.

2. *Image generation*: Venice `/image/generate`. The key comes from
   VENICE_API_KEY, else ~/.config/litellm/litellm.env — same file the
   gateway reads, so no new secret needs configuring.

Config (env):
  MEDIA_VISUAL_MODEL    Venice image model (default z-image-turbo — fast,
                        the image should land mid-utterance, not minutes late)
  MEDIA_VISUAL_STYLE    style suffix appended to every prompt
  MEDIA_VISUAL_SIZE     WxH (default 1024x1024; the canvas cover-crops)
  MEDIA_VISUAL_TIMEOUT  image request timeout seconds (default 90)
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

VENICE_GENERATE_URL = "https://api.venice.ai/api/v1/image/generate"
DEFAULT_MODEL = "z-image-turbo"
DEFAULT_STYLE = "cinematic digital painting, rich colour, soft volumetric light"
DEFAULT_SIZE = "1024x1024"
DEFAULT_TIMEOUT = 90

PROMPT_SHAPER = (
    "You turn an assistant's spoken reply into ONE image-generation prompt: "
    "a single vivid scene that captures the reply's essence — its subject, "
    "mood, and outcome — as a concrete visual metaphor. Plain descriptive "
    "language, at most 50 words. Never ask for text, words, letters, "
    "diagrams, or UI in the image. Output only the prompt."
)


def _read_env_file_key(path: str, name: str) -> str:
    try:
        with open(path) as fh:
            for line in fh:
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _venice_key() -> str:
    return (os.environ.get("VENICE_API_KEY")
            or _read_env_file_key(
                os.path.expanduser("~/.config/litellm/litellm.env"),
                "VENICE_API_KEY"))


def _shape_timeout() -> int:
    """Shaping is one call per reply and the image already lands mid-utterance,
    so inherit the (long, local-model-sized) summary timeout by default."""
    for var in ("MEDIA_VISUAL_SHAPE_TIMEOUT", "MEDIA_SUMMARY_TIMEOUT"):
        try:
            v = int(os.environ.get(var, "") or 0)
        except ValueError:
            v = 0
        if v > 0:
            return v
    return 30


def _gateway_chat(system_prompt: str, user_text: str, timeout: int) -> str | None:
    """One chat turn against the summary gateway. None on any failure —
    identical resolution order to core intake/_summary.py so the two paths
    always target the same endpoint."""
    base = (os.environ.get("MEDIA_SUMMARY_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1").rstrip("/")
    key = (os.environ.get("MEDIA_SUMMARY_API_KEY")
           or os.environ.get("LITELLM_MASTER_KEY")
           or _read_env_file_key(
               os.path.expanduser("~/.config/litellm/litellm.env"),
               "LITELLM_MASTER_KEY")
           or os.environ.get("OPENAI_API_KEY") or "")
    model = (os.environ.get("MEDIA_VISUAL_SHAPE_MODEL")
             or os.environ.get("MEDIA_SUMMARY_MODEL") or "gpt-4o-mini")
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": user_text}],
        "temperature": 0.7,
    }).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        out = (data["choices"][0]["message"]["content"] or "").strip()
    except (urllib.error.URLError, OSError, ValueError, KeyError,
            IndexError, TypeError, json.JSONDecodeError):
        return None
    return out or None


def shape_prompt(reply_text: str) -> tuple[str, bool]:
    """(image prompt, used_llm). The style suffix is always appended so the
    canvas keeps one visual voice across replies."""
    style = os.environ.get("MEDIA_VISUAL_STYLE") or DEFAULT_STYLE
    shaped = _gateway_chat(PROMPT_SHAPER, reply_text.strip()[:4000],
                           _shape_timeout())
    if shaped:
        return f"{shaped} {style}", True
    return f"{reply_text.strip()[:300]} {style}", False


def _size() -> tuple[int, int]:
    raw = os.environ.get("MEDIA_VISUAL_SIZE") or DEFAULT_SIZE
    try:
        w, h = raw.lower().split("x", 1)
        return int(w), int(h)
    except ValueError:
        return 1024, 1024


def generate_image(prompt: str, *, model: str | None = None) -> tuple[bytes | None, str]:
    """Generate one image via Venice. Returns (webp bytes, "") or (None, err)."""
    key = _venice_key()
    if not key:
        return None, "VENICE_API_KEY not set (env or ~/.config/litellm/litellm.env)"
    model = model or os.environ.get("MEDIA_VISUAL_MODEL") or DEFAULT_MODEL
    w, h = _size()
    timeout = int(os.environ.get("MEDIA_VISUAL_TIMEOUT") or DEFAULT_TIMEOUT)
    body = json.dumps({
        "model": model,
        "prompt": prompt[:1500],
        "width": w,
        "height": h,
        "format": "webp",
        "hide_watermark": True,
    }).encode()
    req = urllib.request.Request(
        VENICE_GENERATE_URL, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except OSError:
            pass
        return None, f"venice http {e.code}: {detail}"
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as e:
        return None, f"venice: {e}"
    images = data.get("images") or []
    if not images:
        return None, f"venice response missing images: {json.dumps(data)[:200]}"
    try:
        return base64.b64decode(images[0]), ""
    except (ValueError, TypeError) as e:
        return None, f"venice b64: {e}"
