"""Audio-feature retrieval from your own library — config-gated.

The idea: analyze the tracks you own once (BPM, energy, valence, acousticness,
instrumentalness…) so the qualities brief can be matched to *measured* tracks
instead of the model's memory. Self-hosted, so there's no API to lose (Spotify's
audio-features endpoint was deprecated Nov 2024).

Dormant until two things exist:
  1. a feature extractor — `pip install 'astrotunes[features]'` (librosa), or Essentia
  2. a candidate pool — import music into beets (`beet import <path>`); it's empty now

When live, this would return per-candidate feature vectors and a match score
against the brief. The contract is here; the indexing job is the next build.
"""
from __future__ import annotations

from typing import Any

NAME = "features"


def _has_extractor() -> str | None:
    for lib in ("librosa", "essentia"):
        try:
            __import__(lib)
            return lib
        except ImportError:
            continue
    return None


def _beets_count() -> int | None:
    """Tracks in the beets library, or None if beets isn't reachable."""
    try:
        import subprocess
        out = subprocess.run(["beet", "ls", "-f", "$id"],
                             capture_output=True, text=True, timeout=8)
        if out.returncode != 0:
            return None
        return sum(1 for ln in out.stdout.splitlines() if ln.strip())
    except (OSError, Exception):
        return None


def contribute(ctx: dict[str, Any]) -> dict[str, Any]:
    extractor = _has_extractor()
    count = _beets_count()

    if extractor is None:
        return {"available": False,
                "status": "configure: pip install 'astrotunes[features]' (librosa) "
                          "or install Essentia"}
    if not count:
        return {"available": False,
                "status": f"configure: beets library is empty ({extractor} ready) "
                          "— `beet import <music>` to build a candidate pool"}

    # Live path (indexing job TBD): measured features → match against the brief.
    return {
        "available": False,
        "status": f"ready: {extractor} + {count} tracks; feature index not built yet "
                  "(run the indexer — next build)",
        "data": {"extractor": extractor, "library_tracks": count},
    }
