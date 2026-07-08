"""Visual engines: venice (built-in) + third-party via entry points.

Mirrors core's render-engine seam (agent_media_core/extensions.py): `venice`
is the only engine this package ships and the default; any other engine is an
installable plugin registered under the ``agent_media.visual_engines``
entry-point group and discovered at runtime — this package imports none of
them.

**The contract** is one callable:

    def generate(prompt: str) -> tuple[bytes | None, str]:
        '''Generate one image for `prompt`. Return (image bytes, "") or
        (None, "<why>").'''

Read all config (API key, model, size) from ``os.environ`` inside the engine
— nothing is passed in, so engines stay self-describing. Convention:
``MEDIA_VISUAL_MODEL_<ENGINE>`` for the engine's default model.

Register it:

    [project.entry-points."agent_media.visual_engines"]
    myengine = "my_package.module:generate"

Select it with ``MEDIA_VISUAL_ENGINE=myengine``. A non-venice failure —
including an engine that isn't installed — falls back to
``MEDIA_VISUAL_FALLBACK_ENGINE`` (default venice) so a reply is never
silently unillustrated while either backend is up.

Rules (same as core's registry): an extension may not shadow a built-in name
(the collision is logged and the built-in wins); a broken extension is logged
and skipped, never fatal; discovery is cached per process (``refresh=True``
re-scans, mainly for tests).
"""

from __future__ import annotations

import logging
import os
from importlib.metadata import entry_points
from typing import Callable, Dict

log = logging.getLogger(__name__)

VISUAL_ENGINE_GROUP = "agent_media.visual_engines"
BUILTIN_ENGINE_NAMES = ("venice",)

VisualEngine = Callable[[str], "tuple[bytes | None, str]"]

_cache: Dict[str, VisualEngine] | None = None


def discover_visual_engines(refresh: bool = False) -> Dict[str, VisualEngine]:
    """``{name: engine}`` for all installed third-party visual engines.

    Built-in names are filtered out (dispatched directly, not via the
    registry). A failed import or duplicate name is logged and skipped rather
    than raised — one broken extension must not take down the canvas.
    """
    global _cache
    if _cache is not None and not refresh:
        return _cache

    found: Dict[str, VisualEngine] = {}
    for ep in entry_points(group=VISUAL_ENGINE_GROUP):
        if ep.name in BUILTIN_ENGINE_NAMES:
            log.warning(
                "extension %r tries to shadow built-in visual engine %r; ignoring",
                getattr(ep, "value", ep), ep.name,
            )
            continue
        if ep.name in found:
            log.warning("duplicate visual engine %r; keeping the first", ep.name)
            continue
        try:
            fn = ep.load()
        except Exception as e:  # noqa: BLE001 — one bad plugin must not break us
            log.warning("visual engine %r failed to load: %s", ep.name, e)
            continue
        if not callable(fn):
            log.warning("visual engine %r is not callable; skipping", ep.name)
            continue
        found[ep.name] = fn

    _cache = found
    return found


def all_engine_names() -> tuple[str, ...]:
    """Built-in engines plus any discovered third-party ones."""
    return BUILTIN_ENGINE_NAMES + tuple(discover_visual_engines())


def _generate_one(prompt: str, engine: str) -> tuple[bytes | None, str]:
    """Generate via a single engine, no fallback."""
    if engine == "venice":
        from .generate import generate_venice
        return generate_venice(prompt)
    ext = discover_visual_engines().get(engine)
    if ext is None:
        return None, f"unknown visual engine: {engine!r} (not installed?)"
    try:
        return ext(prompt)
    except Exception as e:  # noqa: BLE001 — isolate plugin faults; fall back below
        return None, f"visual engine {engine!r} raised: {e}"


def generate_image(prompt: str, *, engine: str | None = None) -> tuple[bytes | None, str]:
    """Generate one image via the selected engine, with fallback.

    Engine: the argument, else ``MEDIA_VISUAL_ENGINE``, else venice. On a
    primary failure the render falls back to ``MEDIA_VISUAL_FALLBACK_ENGINE``
    (default venice); a fallback equal to the primary is a no-op. Returns
    (bytes, "") or (None, "<primary err> | fallback <name>: <err>").
    """
    primary = engine or os.environ.get("MEDIA_VISUAL_ENGINE") or "venice"
    img, err = _generate_one(prompt, primary)
    if img is not None:
        return img, ""
    fallback = os.environ.get("MEDIA_VISUAL_FALLBACK_ENGINE") or "venice"
    if fallback == primary:
        return None, err
    log.warning("visual engine %r failed (%s); falling back to %r",
                primary, err, fallback)
    fb_img, fb_err = _generate_one(prompt, fallback)
    if fb_img is not None:
        return fb_img, ""
    return None, f"{err} | fallback {fallback!r}: {fb_err}"
