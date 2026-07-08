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

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request

DEFAULT_MIN_CHARS = 320
# Long enough for an svg *figure* (the purposeful medium, ~40-75s via the
# gateway) — the author chose to hold for full illustrative effect; the
# timeout is the safety net, not the plan.
DEFAULT_REVEAL_TIMEOUT = 75
CAPTION_MAX = 140

# Inline markers an author writes into a reply to make its visual
# *purposeful* instead of ambient:
#   [[visual: description]]  the description IS the image spec (a figure,
#                            drawn to say something — labels allowed)
#   [[reveal: description]]  same, and speech HOLDS at this exact point
#                            until the canvas shows the image (or timeout)
# Markers are always stripped from the spoken text, visual features on or
# off.
_MARKER = re.compile(r"\[\[\s*(visual|reveal)\s*:\s*(.+?)\s*\]\]",
                     re.IGNORECASE | re.DOTALL)


def extract_visual_markers(raw: str) -> tuple[str, str, str | None, str | None]:
    """(clean_raw, hint, pre_raw, post_raw).

    `clean_raw` is the reply with every marker removed — what should be
    spoken and deduped. `hint` is the first marker's description ("" if
    none). For a `reveal` marker, `pre_raw`/`post_raw` are the reply's
    halves around the split point (still markdown; the hook strips them
    separately); both None otherwise."""
    m = _MARKER.search(raw or "")
    if not m:
        return raw, "", None, None
    hint = " ".join(m.group(2).split())
    clean = _MARKER.sub(" ", raw)
    if m.group(1).lower() == "reveal":
        pre = _MARKER.sub(" ", raw[:m.start()])
        post = _MARKER.sub(" ", raw[m.end():])
        return clean, hint, pre, post
    return clean, hint, None, None


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


def spawn_visual(raw_reply: str, spoken_text: str, session: str = "",
                 hint: str = "") -> None:
    """Fire-and-forget `media-visual` for this reply. Never raises, never
    blocks: any problem (no binary, spawn failure) is not playback's concern.
    `session` keys the canvas's scene-continuity memory — consecutive replies
    from one session evolve a single artwork. A non-empty `hint` (from a
    [[visual:]]/[[reveal:]] marker) makes the image purposeful: the hint is
    the spec, drawn as a figure."""
    exe = shutil.which("media-visual")
    if not exe:
        return
    argv = [exe, "--caption", _caption(spoken_text)]
    if session:
        argv += ["--session", session]
    if hint:
        argv += ["--hint", hint]
    argv.append(raw_reply)
    try:
        subprocess.Popen(
            argv,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


# --- the reveal wait: speech holds until the canvas shows the image -----------

def reveal_timeout() -> int:
    try:
        v = int(os.environ.get("MEDIA_VISUAL_REVEAL_TIMEOUT", "")
                or DEFAULT_REVEAL_TIMEOUT)
        return v if v > 0 else DEFAULT_REVEAL_TIMEOUT
    except ValueError:
        return DEFAULT_REVEAL_TIMEOUT


def _canvas_last_url() -> str:
    raw = os.environ.get("MEDIA_VISUAL_URL") or "http://127.0.0.1:8781"
    first = raw.replace(",", " ").split()[0].rstrip("/")
    return first + "/last"


def wait_for_fresh_visual(after_epoch: float,
                          timeout_s: int | None = None) -> bool:
    """Poll the canvas until it reports an image newer than `after_epoch`.
    True = the picture is up; False = timed out / canvas unreachable —
    the caller speaks on regardless (a hung generator must never mute a
    reply mid-sentence). Runs only in the detached playback child."""
    deadline = time.time() + (timeout_s if timeout_s is not None
                              else reveal_timeout())
    url = _canvas_last_url()
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                t = float(json.loads(resp.read()).get("t") or 0)
            if t >= after_epoch:
                return True
        except Exception:  # noqa: BLE001 — polling; any failure just retries
            pass
        time.sleep(1)
    return False
