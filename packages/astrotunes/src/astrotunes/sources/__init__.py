"""Pluggable information sources for music selection.

Each source is a small module exposing `NAME` and `contribute(ctx) -> dict`.
A source looks at the assembled context (time, natal, transits, Human Design,
weather, …) and returns a contribution with any of:

    {
      "available":    bool,                  # did it actually produce anything
      "status":       str,                   # "live" | "configure X to enable"
      "summary":      str,                   # one human line
      "brief_nudges": {axis: float},         # fold into the qualities brief
      "themes":       [str],                 # flavour keywords for the picker
      "seeds":        [{"query","why"}],     # concrete search seeds (tracks)
      "data":         {...},                 # source-specific payload
    }

The point is that "all of them" — esoteric vocabularies, your own library's
audio features, listening history, biometric/calendar context — plug into one
frame and *degrade gracefully*: a source with no creds/data returns
`available: False` with a status telling you how to switch it on, instead of
crashing the brief.

MCP-only signals (Google Calendar, Spotify now-playing) are NOT sources — the
CLI can't call MCP servers. Those live in the music-transit skill, which runs
inside Claude. This registry is for what the CLI can compute or fetch directly.
"""
from __future__ import annotations

from typing import Any

# Import order = display order. Live (pure-data) first, then config-gated.
from . import raga, genekeys, sabian, features, lastfm, ha

_SOURCES = [raga, genekeys, sabian, features, lastfm, ha]


def gather(ctx: dict[str, Any]) -> dict[str, Any]:
    """Run every registered source over the context; never raise."""
    out: dict[str, Any] = {}
    for mod in _SOURCES:
        name = getattr(mod, "NAME", mod.__name__.rsplit(".", 1)[-1])
        try:
            out[name] = mod.contribute(ctx)
        except Exception as e:  # a broken source must not sink the rest
            out[name] = {"available": False, "status": f"error: {e}"}
    return out


def collected_nudges(sources: dict[str, Any]) -> dict[str, float]:
    """Sum brief_nudges across all available sources (qualities caps them)."""
    total: dict[str, float] = {}
    for contrib in sources.values():
        if not contrib.get("available"):
            continue
        for axis, delta in (contrib.get("brief_nudges") or {}).items():
            total[axis] = total.get(axis, 0.0) + delta
    return total
