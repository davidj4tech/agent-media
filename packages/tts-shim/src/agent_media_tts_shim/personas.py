"""SillyTavern persona portraits on the canvas.

When a persona speaks — a TTS request whose `voice` names a character that has a
portrait set — show that character's face on the canvas instead of a generated
figure. The character is *present* on the wall, not just a disembodied voice.
An optional lexical emotion pick swaps expression sprites.

Portrait store — MEDIA_PERSONA_DIR (default ~/.config/agent-media/personas):

    <slug>/neutral.png                    # the fallback expression
    <slug>/{happy,sad,angry,surprised}.png   # optional variants

<slug> is the persona's TTS `voice`, slugified. The canvas serves these at
/persona/<slug>/<file> (canvas.py::_persona), so the shim only pushes URLs — it
never moves image bytes. Shim and canvas share the host filesystem, so the shim
resolves which file exists, then references its canvas URL.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

_EXTS = ("png", "webp", "jpg", "jpeg", "gif")

# Deliberately crude lexical cues → sprite name; swap for SillyTavern's own
# classifier or a model later. Iterated in priority order.
_EMOTION_CUES = (
    ("angry", ("furious", " angry", "how dare", "!!!", "grr")),
    ("sad", (" sad", "sorry", "unfortunately", "afraid", "regret")),
    ("surprised", ("whoa", "really?", "no way", "surprised", "?!")),
    ("happy", ("haha", "great", " love", "wonderful", "excited", ":)", "yay")),
)


def persona_dir() -> Path:
    base = os.environ.get("MEDIA_PERSONA_DIR") or str(
        Path.home() / ".config" / "agent-media" / "personas")
    return Path(base)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower()).strip("-")


def _emotion(text: str) -> str:
    if (os.environ.get("MEDIA_PERSONA_EMOTION") or "1") != "1":
        return "neutral"
    low = " " + (text or "").lower() + " "
    for emo, cues in _EMOTION_CUES:
        if any(c in low for c in cues):
            return emo
    return "neutral"


def _find(d: Path, name: str) -> str | None:
    for ext in _EXTS:
        f = d / f"{name}.{ext}"
        if f.is_file():
            return f.name
    return None


def resolve(persona: str, text: str) -> tuple[str, str] | None:
    """(slug, filename) for the persona's current expression, or None if this
    voice has no portrait directory."""
    slug = _slug(persona)
    d = persona_dir() / slug
    if not slug or not d.is_dir():
        return None
    fn = _find(d, _emotion(text)) or _find(d, "neutral")
    if not fn:  # a sprite dir with no neutral — take the first image we find
        for p in sorted(d.iterdir()):
            if p.suffix.lstrip(".").lower() in _EXTS:
                fn = p.name
                break
    return (slug, fn) if fn else None


def push(persona: str, text: str, session: str, canvas_url: str) -> bool:
    """Show the persona's portrait on the canvas. Returns True if one was
    pushed (so the caller can skip the generated-figure path)."""
    got = resolve(persona, text)
    if not got:
        return False
    slug, fn = got
    payload = json.dumps({
        "image": f"/persona/{slug}/{fn}",
        "purpose": "portrait",
        "session": session,
        "caption": persona,
    }).encode("utf-8")
    req = urllib.request.Request(
        canvas_url.rstrip("/") + "/show",
        data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=4).close()
        return True
    except (urllib.error.URLError, OSError):
        return False
