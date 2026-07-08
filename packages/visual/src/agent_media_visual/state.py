"""Spool + scene state for the visual channel.

The spool is where generated images land (served by the canvas at /img/).
`scenes.json` beside it is the session-continuity memory: the last shaped
scene per session, so the next reply *evolves* the artwork instead of
starting an unrelated picture (see generate.shape_prompt).

Config (env):
  MEDIA_VISUAL_SPOOL_KEEP      newest images kept by gc (default 200)
  MEDIA_VISUAL_CONTINUITY      "0" disables scene continuity (default on)
  MEDIA_VISUAL_CONTINUITY_TTL  seconds a scene stays alive (default 7200 —
                               walk away for the evening and the canvas
                               starts fresh, not from this morning's scene)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

DEFAULT_SPOOL_KEEP = 200
DEFAULT_CONTINUITY_TTL = 7200


def spool_dir() -> Path:
    """Where generated images land: XDG_STATE_HOME/agent-media/visual."""
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    d = root / "agent-media" / "visual"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _int_env(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "") or default)
        return v if v > 0 else default
    except ValueError:
        return default


# --- session-continuity scene memory -----------------------------------------

def continuity_enabled() -> bool:
    return (os.environ.get("MEDIA_VISUAL_CONTINUITY", "1") or "1").strip() != "0"


def continuity_ttl() -> int:
    return _int_env("MEDIA_VISUAL_CONTINUITY_TTL", DEFAULT_CONTINUITY_TTL)


def _scenes_path() -> Path:
    return spool_dir() / "scenes.json"


def _load_scenes() -> dict:
    try:
        with open(_scenes_path()) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def load_scene(session: str) -> str:
    """The session's last shaped scene, or "" when absent/expired/disabled."""
    if not continuity_enabled():
        return ""
    rec = _load_scenes().get(session or "default")
    if not isinstance(rec, dict):
        return ""
    if time.time() - float(rec.get("t") or 0) > continuity_ttl():
        return ""
    return str(rec.get("scene") or "")


def save_scene(session: str, scene: str) -> None:
    """Remember the session's current scene (atomic replace; expired entries
    are pruned on the way through). Best-effort — continuity is never worth
    failing a push over."""
    if not scene:
        return
    try:
        now = time.time()
        ttl = continuity_ttl()
        scenes = {k: v for k, v in _load_scenes().items()
                  if isinstance(v, dict) and now - float(v.get("t") or 0) <= ttl}
        scenes[session or "default"] = {"scene": scene, "t": now}
        tmp = _scenes_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(scenes))
        os.replace(tmp, _scenes_path())
    except (OSError, ValueError):
        pass


# --- spool GC -----------------------------------------------------------------

def gc_spool(keep: int | None = None) -> int:
    """Delete all but the newest `keep` spooled images (default
    MEDIA_VISUAL_SPOOL_KEEP / 200). Returns how many were removed.
    Best-effort — a GC failure must never fail the push that triggered it."""
    if keep is None:
        keep = _int_env("MEDIA_VISUAL_SPOOL_KEEP", DEFAULT_SPOOL_KEEP)
    removed = 0
    try:
        imgs = sorted(spool_dir().glob("img-*"),
                      key=lambda f: f.stat().st_mtime, reverse=True)
        for f in imgs[keep:]:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed
