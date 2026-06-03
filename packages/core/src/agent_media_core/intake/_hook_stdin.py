"""Shared stdin-pipe hook logic for agent harnesses (codex, pi, …).

Each harness hook is a one-liner that calls `run()` with its source and
the prefix used for its per-source env var overrides.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from ..types import Event, Priority, Source
from ._text import strip_markdown
from .submit import submit_event

log = logging.getLogger(__name__)


def _load_env_file(label: str) -> None:
    from ._env import load_env_file
    load_env_file(label)


def run(source: Source, env_prefix: str) -> int:
    """Read stdin, strip markdown, submit as *source*.

    *env_prefix* is the upper-case harness name (e.g. ``"CODEX"`` or
    ``"PI"``). The hook checks ``<PREFIX>_TTS_ENABLED``, and honours
    ``<PREFIX>_TTS_ENGINE`` / ``<PREFIX>_TTS_VOICE`` overrides before
    falling back to the generic ``MEDIA_RENDER_*`` vars.
    """
    if os.environ.get("MEDIA_HOOK_ENABLED", "1") == "0":
        return 0
    if os.environ.get(f"{env_prefix}_TTS_ENABLED", "1") == "0":
        return 0

    _load_env_file(f"hook-{env_prefix.lower()}")

    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        return 0

    cleaned = strip_markdown(raw)
    if not cleaned:
        return 0

    engine = (os.environ.get(f"{env_prefix}_TTS_ENGINE")
              or os.environ.get("MEDIA_RENDER_ENGINE"))
    # Per-source voice override only — engine-specific (an optional
    # <PREFIX>_TTS_VOICE_<ENGINE> form wins over the bare <PREFIX>_TTS_VOICE).
    # The generic MEDIA_RENDER_VOICE is intentionally NOT folded in here: that
    # would force one engine's voice onto another. submit._resolve_voice picks
    # the right per-engine voice when this is None.
    voice = None
    if engine:
        voice = os.environ.get(
            f"{env_prefix}_TTS_VOICE_{engine.upper().replace('-', '_')}")
    voice = voice or os.environ.get(f"{env_prefix}_TTS_VOICE")

    submit_event(Event(
        text=cleaned,
        source=source,
        priority=Priority.NORMAL,
        engine=engine,
        voice=voice,
        metadata={"kind": "stop"},
    ))
    return 0
