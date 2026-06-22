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

import subprocess
from pathlib import Path
from typing import Callable, Optional


EDGE_DEFAULT_VOICE = "en-US-AriaNeural"


def _render_edge(text: str, outfile: Path, *, voice: str, edge_bin: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [edge_bin, "--text", text, "--voice", voice, "--write-media", str(outfile)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    err = proc.stderr.decode(errors="replace").strip()
    ok = proc.returncode == 0 and outfile.exists() and outfile.stat().st_size > 0
    return ok, err


# The only engine bundled with core. Everything else arrives via entry points;
# `extensions.all_engine_names()` reports built-ins + installed plugins.
KNOWN_ENGINES = ("edge",)


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
    """Render `text` to `outfile` via `engine`. Returns (ok, err).

    `edge` is handled directly; any other engine is resolved from the
    `agent_media.render_engines` entry-point registry. On a non-edge failure
    (engine errored, raised, or isn't installed) with `fallback_to_edge=True`,
    falls back to edge. `on_fallback(engine, err)` is called so callers can log
    the original engine's error.
    """
    if engine == "edge":
        return _render_edge(text, outfile,
                            voice=voice or edge_voice, edge_bin=edge_bin)

    # Non-edge engines are optional plugins discovered via entry points.
    from ..extensions import get_render_engine
    ext = get_render_engine(engine)
    if ext is None:
        ok, err = False, f"unknown engine: {engine!r} (not installed?)"
    else:
        try:
            ok, err = ext(text, outfile, voice=voice)
        except Exception as e:  # noqa: BLE001 — isolate plugin faults; fall back below
            ok, err = False, f"engine {engine!r} raised: {e}"

    if not ok and fallback_to_edge:
        if on_fallback is not None:
            on_fallback(engine, err)
        return _render_edge(text, outfile, voice=edge_voice, edge_bin=edge_bin)
    return ok, err
