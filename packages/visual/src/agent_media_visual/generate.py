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
import re
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
    """(scene prompt, used_llm). Evolves the session's previous scene when one
    is alive (see module docstring). The scene is *style-free* — each engine
    applies its own aesthetic (venice appends MEDIA_VISUAL_STYLE, svg speaks
    clip-art natively), so one scene memory serves every engine."""
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
        # Remember the scene as the next evolution base. A fallback raw-text
        # prompt is NOT a scene — don't poison the memory with it.
        state.save_scene(session, shaped)
        return shaped, True
    return text[:300], False


def _size() -> tuple[int, int]:
    raw = os.environ.get("MEDIA_VISUAL_SIZE") or DEFAULT_SIZE
    try:
        w, h = raw.lower().split("x", 1)
        return int(w), int(h)
    except ValueError:
        return 1024, 1024


def generate_venice(prompt: str) -> tuple[bytes | None, str]:
    """The built-in raster engine: one image via Venice `/image/generate`.
    Returns (webp bytes, "") or (None, err). Matches the engines.py contract.
    The raster style suffix (MEDIA_VISUAL_STYLE) is applied here — the scene
    prompt arrives style-free so other engines can speak their own aesthetic."""
    key = _venice_key()
    if not key:
        return None, "VENICE_API_KEY not set (env or ~/.config/litellm/litellm.env)"
    prompt = f"{prompt} {os.environ.get('MEDIA_VISUAL_STYLE') or DEFAULT_STYLE}"
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


# --- beats: storyboard one scene across the parts of a reply ------------------

PROMPT_STORY = (
    "You illustrate an assistant's spoken reply as ONE scene, then "
    "storyboard that scene across the reply's numbered parts. For N parts, "
    "output EXACTLY N+1 lines and nothing else. Line 1: one vivid "
    "image-generation prompt capturing the whole reply's essence as a "
    "concrete visual metaphor (at most 50 words); if a previous scene is "
    "given, line 1 must EVOLVE it — keep its setting, palette, and main "
    "subject where they still fit, change what the reply changes. Lines 2 "
    "to N+1: that same scene at the moment of each part, in order — same "
    "setting, palette, and main subject; change only what the part changes "
    "(at most 40 words each). Never ask for text, words, letters, diagrams, "
    "or UI. Describe scenes directly, no meta-language. No numbering, no "
    "blank lines, no quotes."
)

_LINE_PREFIX = re.compile(r"^\s*(?:\d+\s*[).:\-]|[-*•])\s*")


def _clean_lines(raw: str) -> list[str]:
    return [_LINE_PREFIX.sub("", ln).strip().strip('"').strip("'")
            for ln in raw.splitlines() if ln.strip()]


def shape_story(reply_text: str, parts: list[str], *, session: str = ""
                ) -> tuple[str, list[str] | None, bool]:
    """(scene, beat prompts | None, used_llm) in ONE gateway call — scene and
    storyboard together, because two round-trips on a slow model would outlast
    the speech the beats are meant to accompany. Continuity works exactly as
    in shape_prompt: the previous scene is offered for evolution and the new
    scene is saved. Degrades gracefully: a line-count mismatch keeps the first
    line as the scene (no beats); no output falls back to the raw text."""
    text = reply_text.strip()[:4000]
    user = f"Reply:\n{text}\n\nParts:\n" + "\n".join(
        f"{i + 1}) {p.strip()[:400]}" for i, p in enumerate(parts))
    prev = state.load_scene(session)
    if prev:
        user = f"Previous scene:\n{prev}\n\n{user}"
    out = _gateway_chat(PROMPT_STORY, user, _shape_timeout())
    if not out:
        return text[:300], None, False
    lines = _clean_lines(out)
    if not lines:
        return text[:300], None, False
    scene = lines[0]
    state.save_scene(session, scene)
    beats = lines[1:1 + len(parts)]
    if len(beats) < len(parts):
        return scene, None, True
    return scene, beats, True


# --- the svg engine: animated clip-art straight from the LLM ------------------
# No image API at all: one gateway chat emits a self-contained animated SVG
# (SMIL <animate>/<animateTransform> loops play inside an <img> tag — scripts
# don't, which is also why they're rejected below). Fast on a local model,
# infinitely crisp, and the "movement" is native rather than faked. Aesthetic
# lives in the system prompt (flat clip-art), not MEDIA_VISUAL_STYLE.

PROMPT_SVG = (
    "You are a vector illustrator. Turn the given scene into ONE complete, "
    "self-contained animated SVG, viewBox=\"0 0 1600 900\". Flat clip-art "
    "style: bold simple shapes, a coherent 5-8 colour palette. Compose "
    "full-bleed and layered back-to-front: first a background rect covering "
    "the whole viewBox (sky/room/backdrop, a subtle linearGradient is good), "
    "then setting elements (ground, horizon, large forms), then 10-25 shapes "
    "building the main subject at a generous size near the centre. Animate "
    "3-5 elements with gentle infinite SMIL loops (<animate> / "
    "<animateTransform>, dur 4-12s) — drifting, pulsing, rotating, floating; "
    "each animation element must be INSIDE the shape or <g> it animates. "
    "Rules: no <script>, no <foreignObject>, no external URLs or images, "
    "no <text>. Output ONLY the SVG markup, nothing else."
)

PROMPT_SVG_FIGURE = (
    "You are a technical illustrator. Turn the given description into ONE "
    "complete, self-contained SVG figure that COMMUNICATES it: a clear "
    "diagram, comparison, or illustrative scene — whatever the description "
    "calls for. viewBox=\"0 0 1600 900\", flat design, bold simple shapes, "
    "a coherent 5-8 colour palette, generous sizing (it is viewed from "
    "across a room). Short text labels ARE allowed and encouraged where "
    "they carry meaning — sans-serif, large (36px+), high contrast, never "
    "overlapping. Animate 1-3 elements subtly with infinite SMIL loops "
    "(<animate> / <animateTransform>, dur 4-12s) — a gentle pulse on the "
    "key element, a slow drift — never distracting from the content. "
    "Rules: no <script>, no <foreignObject>, no external URLs or images. "
    "Output ONLY the SVG markup, nothing else."
)

_SVG_FORBIDDEN = ("<script", "<foreignobject", "http://", "https://",
                  "javascript:")
# The one URL family a valid SVG must contain: the W3C namespace declarations
# (xmlns / xmlns:xlink). Blanked out before the forbidden scan.
_SVG_NAMESPACE_OK = "http://www.w3.org/"


def _svg_timeout() -> int:
    """SVG markup is a much longer completion than a one-line prompt; give it
    the image budget, not the shaping budget."""
    try:
        v = int(os.environ.get("MEDIA_VISUAL_SVG_TIMEOUT", "") or 0)
    except ValueError:
        v = 0
    return v if v > 0 else int(os.environ.get("MEDIA_VISUAL_TIMEOUT")
                               or DEFAULT_TIMEOUT)


def _svg_ns_lower() -> str:
    return _SVG_NAMESPACE_OK.lower()


def _extract_svg(raw: str) -> str | None:
    """The <svg>…</svg> span of a completion (models love to wrap markup in
    code fences or preamble), or None."""
    lo = raw.find("<svg")
    hi = raw.rfind("</svg>")
    if lo < 0 or hi < 0:
        return None
    return raw[lo:hi + len("</svg>")]


def generate_svg(prompt: str) -> tuple[bytes | None, str]:
    """The built-in clip-art engine: the gateway LLM emits an animated SVG.
    Returns (svg bytes, "") or (None, err) — a bad completion falls back to
    the raster engine via engines.generate_image. MEDIA_VISUAL_SVG_MODEL
    overrides the model (default: the shaping model).

    MEDIA_VISUAL_FIGURE=1 (set by the CLI for author-hinted, *purposeful*
    visuals) switches to the figure prompt: communicate, not decorate —
    diagrams and labeled comparisons, with crisp text labels allowed (vector
    text renders perfectly; the no-text rule exists for raster models)."""
    system = (PROMPT_SVG_FIGURE
              if os.environ.get("MEDIA_VISUAL_FIGURE") == "1" else PROMPT_SVG)
    if os.environ.get("MEDIA_VISUAL_SVG_MODEL"):
        # _gateway_chat honours MEDIA_VISUAL_SHAPE_MODEL first; route the
        # override through it for this one call.
        saved = os.environ.get("MEDIA_VISUAL_SHAPE_MODEL")
        os.environ["MEDIA_VISUAL_SHAPE_MODEL"] = os.environ["MEDIA_VISUAL_SVG_MODEL"]
        try:
            raw = _gateway_chat(system, prompt, _svg_timeout())
        finally:
            if saved is None:
                os.environ.pop("MEDIA_VISUAL_SHAPE_MODEL", None)
            else:
                os.environ["MEDIA_VISUAL_SHAPE_MODEL"] = saved
    else:
        raw = _gateway_chat(system, prompt, _svg_timeout())
    if not raw:
        return None, "svg: gateway returned nothing"
    svg = _extract_svg(raw)
    if not svg:
        return None, "svg: completion contained no <svg> element"
    low = svg.lower().replace(_svg_ns_lower(), "")
    for bad in _SVG_FORBIDDEN:
        if bad in low:
            return None, f"svg: rejected ({bad!r} present)"
    try:
        import xml.etree.ElementTree as ET
        ET.fromstring(svg)
    except ET.ParseError as e:
        return None, f"svg: not well-formed: {e}"
    return svg.encode("utf-8"), ""
