"""Extension discovery for agent-media.

agent-media is a small core plus optional, independently-installable pieces.
This module is the *contract*: how a third-party package plugs new behaviour
into core without core importing it.

Three seams, in decreasing order of how plug-and-play they are:

1. Render engines — Python entry points, group ``agent_media.render_engines``.
   This module discovers them and ``render.render_text`` dispatches to them.
   A package adds:

       [project.entry-points."agent_media.render_engines"]
       myengine = "my_pkg.engine:render"

   where ``render`` matches `RenderEngine` below. See docs/EXTENSIONS.md.

2. Intake adapters — already pluggable at the process level: each adapter is
   its own console-script entry point (``media-intake-*`` / ``media-hook-*``)
   that builds an Event and calls ``intake.submit``. A new intake source ships
   its own console script; nothing here needs to change. Documented, not
   discovered — core never has to import an intake adapter to use it.

3. Sinks (speech / music / book) — the three fixed channels are core identity,
   not a third-party seam (yet). Listed here only so the boundary is explicit.

Discovery is lazy and cached: the first lookup reads entry points once; pass
``refresh=True`` (mainly for tests) to re-scan.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from pathlib import Path
from typing import Callable, Dict, Protocol, runtime_checkable

log = logging.getLogger(__name__)

# Entry-point group third-party render engines register under.
RENDER_ENGINE_GROUP = "agent_media.render_engines"

# Built-in engine names render_text handles directly. Only `edge` ships with
# core now (openai/qwen/realtime moved to their own packages). A discovered
# engine may not shadow a built-in — the collision is logged and the built-in
# wins, so a stray third-party package can never silently replace core's TTS.
BUILTIN_ENGINE_NAMES = ("edge",)


@runtime_checkable
class RenderEngine(Protocol):
    """A text-to-audio render engine.

    Same shape as core's built-in engines: render ``text`` to ``outfile`` and
    return ``(ok, error_message)``. ``voice`` is the caller's requested voice
    (or None → use the engine's own default). Read any other config (API keys,
    model, base URL) from ``os.environ`` inside the engine — core passes none of
    it, so engines stay self-describing and core stays decoupled.
    """

    def __call__(
        self, text: str, outfile: Path, *, voice: str | None = None
    ) -> tuple[bool, str]:
        ...


_cache: Dict[str, RenderEngine] | None = None


def discover_render_engines(refresh: bool = False) -> Dict[str, RenderEngine]:
    """Return ``{name: engine}`` for all installed third-party render engines.

    Built-in names are filtered out (they're dispatched directly, not via the
    registry). A failed import or a duplicate name is logged and skipped rather
    than raised — one broken extension must not take down rendering.
    """
    global _cache
    if _cache is not None and not refresh:
        return _cache

    found: Dict[str, RenderEngine] = {}
    for ep in entry_points(group=RENDER_ENGINE_GROUP):
        if ep.name in BUILTIN_ENGINE_NAMES:
            log.warning(
                "extension %r tries to shadow built-in engine %r; ignoring",
                getattr(ep, "value", ep), ep.name,
            )
            continue
        if ep.name in found:
            log.warning("duplicate render engine %r; keeping the first", ep.name)
            continue
        try:
            fn = ep.load()
        except Exception as e:  # noqa: BLE001 — one bad plugin must not break core
            log.warning("render engine %r failed to load: %s", ep.name, e)
            continue
        if not callable(fn):
            log.warning("render engine %r is not callable; skipping", ep.name)
            continue
        found[ep.name] = fn

    _cache = found
    return found


def all_engine_names() -> tuple[str, ...]:
    """Built-in engines plus any discovered third-party ones, for listing /
    validation."""
    return BUILTIN_ENGINE_NAMES + tuple(discover_render_engines())


def get_render_engine(name: str) -> RenderEngine | None:
    """Return a discovered third-party engine by name, or None."""
    return discover_render_engines().get(name)
