"""Optional visual accompaniment for spoken replies (MEDIA_SPEECH_VISUAL=1).

When enabled, the Stop hook hands the raw reply to `media-visual` (the
agent-media-visual package's CLI), which shapes it into an image prompt,
generates a picture, and pushes it to the visual canvas — the web page
served by `media-visual-canvas` that a phone/TV browser leaves open.

Core stays decoupled from the optional package the same way it avoids
importing render-engine plugins: this module only *spawns the console
script if it exists on PATH*. No binary → silent no-op. The spawn is
fire-and-forget (own session, stdio detached) so speech never waits on
pixels; the image cross-fades in mid-utterance when it's ready.

Config (env / ~/.config/agent-media.env):
  MEDIA_SPEECH_VISUAL      "1" to enable (default off)
  MEDIA_VISUAL_MIN_CHARS   only illustrate replies at least this long
                           (default 320, matching the summary threshold —
                           one-liners aren't worth a picture)
"""

from __future__ import annotations

import os
import shutil
import subprocess

DEFAULT_MIN_CHARS = 320
CAPTION_MAX = 140


def visual_enabled() -> bool:
    return (os.environ.get("MEDIA_SPEECH_VISUAL", "0") or "0").strip() == "1"


def visual_min_chars() -> int:
    try:
        return int(os.environ.get("MEDIA_VISUAL_MIN_CHARS", "") or DEFAULT_MIN_CHARS)
    except ValueError:
        return DEFAULT_MIN_CHARS


def _caption(spoken_text: str) -> str:
    """First stretch of the spoken text, cut at a word boundary — grounds the
    image without covering it in prose."""
    t = " ".join((spoken_text or "").split())
    if len(t) <= CAPTION_MAX:
        return t
    cut = t[:CAPTION_MAX].rsplit(" ", 1)[0]
    return (cut or t[:CAPTION_MAX]) + "…"


def spawn_visual(raw_reply: str, spoken_text: str) -> None:
    """Fire-and-forget `media-visual` for this reply. Never raises, never
    blocks: any problem (no binary, spawn failure) is not playback's concern."""
    exe = shutil.which("media-visual")
    if not exe:
        return
    try:
        subprocess.Popen(
            [exe, "--caption", _caption(spoken_text), raw_reply],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass
