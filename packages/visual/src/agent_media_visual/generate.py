"""Reply text → image prompt (with scene continuity) → the venice engine.

Prompt shaping is one chat call to the same OpenAI-compatible gateway the
summary/describe path uses (MEDIA_SUMMARY_BASE_URL + LiteLLM master key —
mirrors core's intake/_summary.py resolution). Two modes:

* First reply of a session: describe ONE vivid scene for the reply.
* Later replies (within MEDIA_VISUAL_CONTINUITY_TTL): *evolve* the previous
  scene to reflect the new reply — the canvas becomes one continuously
  developing artwork instead of a slideshow of unrelated pictures. The last
  shaped scene per session lives in the spool's scenes.json (see state.py).

Falls back to the raw text on any shaping failure (and doesn't poison the
scene memory with it).

`generate_venice` is the built-in visual engine (see engines.py for the
pluggable seam and the fallback rules). Its key comes from VENICE_API_KEY,
else ~/.config/litellm/litellm.env — the same file the gateway reads, so no
new secret needs configuring.

Config (env):
  MEDIA_VISUAL_MODEL_VENICE  venice image model (also MEDIA_VISUAL_MODEL for
                             back-compat; default z-image-turbo — fast, the
                             image should land mid-utterance, not minutes late)
  MEDIA_VISUAL_STYLE    style suffix appended to every prompt
  MEDIA_VISUAL_SIZE     WxH (default 1024x1024; the canvas cover-crops)
  MEDIA_VISUAL_TIMEOUT  image request timeout seconds (default 90)
  MEDIA_VISUAL_SHAPE_MODEL / MEDIA_VISUAL_SHAPE_TIMEOUT
                        prompt-shaping overrides (default: the summary
                        model / timeout)
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

from . import state

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

PROMPT_EVOLVER = (
    "You maintain one continuously evolving artwork accompanying an "
    "assistant's spoken replies. Given the previous scene and a new reply, "
    "output ONE image-generation prompt that EVOLVES the previous scene to "
    "reflect the new reply: keep its setting, palette, and main subject where "
    "they still fit; change, add, or remove what the reply changes. Concrete "
    "visual language, at most 60 words. Never ask for text, words, letters, "
    "diagrams, or UI in the image. Describe the scene directly — do not say "
    "'evolve', mention the previous scene, or repeat these instructions. No "
    "surrounding quotes. Output only the prompt."
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


def shape_prompt(reply_text: str, *, session: str = "") -> tuple[str, bool]:
    """(image prompt, used_llm). Evolves the session's previous scene when one
    is alive (see module docstring); the style suffix is always appended so
    the canvas keeps one visual voice across replies."""
    style = os.environ.get("MEDIA_VISUAL_STYLE") or DEFAULT_STYLE
    text = reply_text.strip()[:4000]
    prev = state.load_scene(session)
    if prev:
        shaped = _gateway_chat(
            PROMPT_EVOLVER,
            f"Previous scene:\n{prev}\n\nNew reply:\n{text}",
            _shape_timeout())
    else:
        shaped = _gateway_chat(PROMPT_SHAPER, text, _shape_timeout())
    if shaped:
        # Small local models sometimes wrap the prompt in quotes or lead with
        # the instruction verb anyway; strip the wrapper, keep the scene.
        shaped = shaped.strip().strip('"').strip("'").strip()
        # Remember the scene itself (sans style suffix) as the next evolution
        # base. A fallback raw-text prompt is NOT a scene — don't poison the
        # memory with it.
        state.save_scene(session, shaped)
        return f"{shaped} {style}", True
    return f"{text[:300]} {style}", False


def _size() -> tuple[int, int]:
    raw = os.environ.get("MEDIA_VISUAL_SIZE") or DEFAULT_SIZE
    try:
        w, h = raw.lower().split("x", 1)
        return int(w), int(h)
    except ValueError:
        return 1024, 1024


def generate_venice(prompt: str) -> tuple[bytes | None, str]:
    """The built-in visual engine: one image via Venice `/image/generate`.
    Returns (webp bytes, "") or (None, err). Matches the engines.py contract."""
    key = _venice_key()
    if not key:
        return None, "VENICE_API_KEY not set (env or ~/.config/litellm/litellm.env)"
    model = (os.environ.get("MEDIA_VISUAL_MODEL_VENICE")
             or os.environ.get("MEDIA_VISUAL_MODEL") or DEFAULT_MODEL)
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
