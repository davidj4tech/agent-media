"""Render engines: edge (built-in) + third-party.

`edge` is the only engine core ships — it's zero-config (no API key) and is the
universal default. Every other engine (openai, qwen, realtime, …) is an
optional package registered under the `agent_media.render_engines` entry-point
group and discovered at runtime (see ../extensions.py and docs/EXTENSIONS.md),
so core imports none of them.

`render_text(..., engine="<name>")` dispatches `edge` directly and any other
name through the extension registry. A non-edge failure — including an engine
that isn't installed — falls back to edge when `fallback_to_edge=True`.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional


EDGE_DEFAULT_VOICE = "en-US-AriaNeural"

# Microsoft's free edge-tts websocket endpoint intermittently rejects the
# handshake with 503/403 (throttling / rotated Sec-MS-GEC token). A single
# failure used to silently drop that sentence — the caller logs it and moves
# on, and since edge is the only engine core ships there's no fallback. The
# failures are transient and clustered, so a short retry-with-backoff recovers
# the sentence instead of leaving a gap in the spoken response. Tunable via
# MEDIA_EDGE_RETRIES (extra attempts) / MEDIA_EDGE_RETRY_BACKOFF_S (base delay).
_EDGE_RETRIES = int(os.environ.get("MEDIA_EDGE_RETRIES", "3"))
_EDGE_RETRY_BACKOFF_S = float(os.environ.get("MEDIA_EDGE_RETRY_BACKOFF_S", "0.6"))


def _render_edge(text: str, outfile: Path, *, voice: str, edge_bin: str) -> tuple[bool, str]:
    err = ""
    for attempt in range(_EDGE_RETRIES + 1):
        proc = subprocess.run(
            [edge_bin, "--text", text, "--voice", voice, "--write-media", str(outfile)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        err = proc.stderr.decode(errors="replace").strip()
        ok = proc.returncode == 0 and outfile.exists() and outfile.stat().st_size > 0
        if ok:
            return True, err
        # Last attempt failed — give up and report the error upstream.
        if attempt >= _EDGE_RETRIES:
            break
        # Exponential backoff so a brief 503/403 throttle window clears before
        # the retry (0.6s, 1.2s, 2.4s by default).
        time.sleep(_EDGE_RETRY_BACKOFF_S * (2 ** attempt))
    return False, err


# The only engine bundled with core. Everything else arrives via entry points;
# `extensions.all_engine_names()` reports built-ins + installed plugins.
KNOWN_ENGINES = ("edge",)


def _render_one(
    text: str,
    outfile: Path,
    *,
    engine: str,
    voice: Optional[str],
    edge_voice: str,
    edge_bin: str,
) -> tuple[bool, str]:
    """Render via a single engine, no fallback. Returns (ok, err).

    `edge` is handled directly; any other engine is resolved from the
    `agent_media.render_engines` entry-point registry.
    """
    if engine == "edge":
        return _render_edge(text, outfile,
                            voice=voice or edge_voice, edge_bin=edge_bin)
    from ..extensions import get_render_engine
    ext = get_render_engine(engine)
    if ext is None:
        return False, f"unknown engine: {engine!r} (not installed?)"
    try:
        return ext(text, outfile, voice=voice)
    except Exception as e:  # noqa: BLE001 — isolate plugin faults; fall back below
        return False, f"engine {engine!r} raised: {e}"


def render_text(
    text: str,
    outfile: Path,
    *,
    engine: str,
    voice: Optional[str] = None,
    edge_voice: str = EDGE_DEFAULT_VOICE,
    edge_bin: str = "edge-tts",
    fallback_to_edge: bool = True,
    on_fallback: Optional[Callable[[str, str], None]] = None,
) -> tuple[bool, str]:
    """Render `text` to `outfile` via `engine`, with a fallback. Returns (ok, err).

    On a primary-engine failure (errored, raised, isn't installed — or, for
    edge, exhausted its retries) the render falls back to a second engine.
    The fallback is `MEDIA_RENDER_FALLBACK_ENGINE` if set, else `edge` when
    `fallback_to_edge=True` (the historical default). This lets an edge-primary
    setup fall through to e.g. `openai` when Microsoft's endpoint 503s persist,
    so a sentence is never silently dropped while *either* provider is up.
    `on_fallback(engine, err)` is called with the primary's error so callers
    can log/notify. A fallback equal to the primary (or unset) is a no-op.
    """
    ok, err = _render_one(text, outfile, engine=engine, voice=voice,
                          edge_voice=edge_voice, edge_bin=edge_bin)
    if ok or not fallback_to_edge:
        # `fallback_to_edge=False` means "no fallback, give me the raw result".
        return ok, err

    # Fallback enabled: use MEDIA_RENDER_FALLBACK_ENGINE if set, else edge.
    fallback = os.environ.get("MEDIA_RENDER_FALLBACK_ENGINE") or "edge"
    if fallback == engine:
        return ok, err

    if on_fallback is not None:
        on_fallback(engine, err)
    # The fallback resolves its own default voice (the primary's voice belongs
    # to the primary's namespace); edge is pinned to edge_voice for back-compat.
    fb_voice = edge_voice if fallback == "edge" else None
    return _render_one(text, outfile, engine=fallback, voice=fb_voice,
                       edge_voice=edge_voice, edge_bin=edge_bin)
