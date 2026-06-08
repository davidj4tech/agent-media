"""Last.fm — your taste + crowd mood tags. Config-gated.

Two music levers Last.fm adds: (1) crowd tags ("chill", "rainy day",
"melancholy") to find tracks by mood, and (2) *your* history — loved tracks and
what you actually reach for — to personalize.

Dormant until you set env vars:
  LASTFM_API_KEY   — free key from https://www.last.fm/api/account/create
  LASTFM_USER      — your username (enables loved-tracks / history)

When live it returns a couple of mood-tag search seeds (from the brief's
strongest theme) plus your recent loved tracks as seeds. No third-party deps —
plain HTTP via httpx.
"""
from __future__ import annotations

import os
from typing import Any

NAME = "lastfm"
API = "https://ws.audioscrobbler.com/2.0/"


def _strong_theme(ctx: dict[str, Any]) -> str | None:
    """A mood word to tag-search by — prefer a raga rasa, else an HD gift."""
    srcs = ctx.get("sources") or {}
    for key in ("raga", "genekeys"):
        themes = (srcs.get(key) or {}).get("themes") or []
        if themes:
            return themes[0]
    return None


def contribute(ctx: dict[str, Any]) -> dict[str, Any]:
    key = os.environ.get("LASTFM_API_KEY")
    user = os.environ.get("LASTFM_USER")
    if not key:
        return {"available": False,
                "status": "configure: set LASTFM_API_KEY (and LASTFM_USER) "
                          "— free key at last.fm/api"}

    try:
        import httpx
    except ImportError:
        return {"available": False, "status": "configure: pip install httpx"}

    seeds: list[dict[str, str]] = []
    notes: list[str] = []

    tag = _strong_theme(ctx)
    try:
        if tag:
            r = httpx.get(API, params={"method": "tag.getTopTracks", "tag": tag,
                                       "api_key": key, "format": "json", "limit": 5},
                          timeout=6)
            for t in (r.json().get("tracks", {}).get("track", []) or [])[:3]:
                seeds.append({"query": f"{t['artist']['name']} {t['name']}",
                              "why": f"Last.fm top track tagged '{tag}'"})
        if user:
            r = httpx.get(API, params={"method": "user.getLovedTracks", "user": user,
                                       "api_key": key, "format": "json", "limit": 10},
                          timeout=6)
            loved = (r.json().get("lovedtracks", {}).get("track", []) or [])
            for t in loved[:3]:
                seeds.append({"query": f"{t['artist']['name']} {t['name']}",
                              "why": "your Last.fm loved track"})
            notes.append(f"{len(loved)} loved tracks read")
    except Exception as e:
        return {"available": False, "status": f"last.fm request failed: {e}"}

    return {
        "available": bool(seeds),
        "status": "live" if seeds else "live (no seeds matched)",
        "summary": f"taste tag '{tag}'; " + ("; ".join(notes) if notes else "no user set"),
        "seeds": seeds,
    }
