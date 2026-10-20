"""The house speakers one by one: which rooms play the music stream, and how
loud (David, 20 Oct 2026: "It would be nice to be able to select a speaker
from the music player").

Every room has a snapclient on the music stream (``MEDIA_DUCK_ROOMS_STREAM``,
e.g. ``am-music``); a room is on when its client is not muted, and its volume
is the client's. The phone's own clients (``MEDIA_SNAP_PHONE_PREFIX``) are not
rooms: the phone is a place of its own in the music picker.

A change made while a reply is ducking the rooms (route/coordinator.py
``_rooms_duck``) goes into the duck's restore baseline rather than over it:
the restore would otherwise put back the volume from before the change, and a
room switched on mid-duck would play at full volume under the voice.
"""

from __future__ import annotations

import os
from typing import Optional

from . import snapcast


def stream() -> str:
    """The music stream the rooms play, or "" when this host has none."""
    return os.environ.get("MEDIA_DUCK_ROOMS_STREAM", "").strip()


def _label(client: dict) -> str:
    name = str((client.get("config") or {}).get("name") or "").strip()
    if name:
        return name
    cid = str(client.get("id") or "")
    return cid[:-len("-music")] if cid.endswith("-music") else cid


def _clients(timeout: float) -> list[dict]:
    """The music stream's clients that are rooms (not the phone's)."""
    s = stream()
    if not s:
        return []
    prefix = snapcast.phone_client_prefix()
    out: list[dict] = []
    for g in snapcast.get_status(timeout=timeout).get("groups", []):
        if g.get("stream_id") != s:
            continue
        for c in g.get("clients", []):
            if prefix and str(c.get("id", "")).startswith(prefix):
                continue
            out.append(c)
    return out


def rooms(timeout: float = 2.0) -> list[dict]:
    """``[{"id", "label", "connected", "on", "volume"}]``, by label.

    The duck's baseline stands in for a ducked room's volume, so the slider
    shows the room's own level, not the one it sits at under a reply. Raises
    SnapcastError when the server cannot be reached.
    """
    from .state import StateStore

    owed = (StateStore().get_rooms_duck() or {}).get("vols") or {}
    out = []
    for c in _clients(timeout):
        vol = (c.get("config") or {}).get("volume") or {}
        cid = str(c.get("id"))
        out.append({"id": cid, "label": _label(c),
                    "connected": bool(c.get("connected")),
                    "on": not vol.get("muted"),
                    "volume": int(owed.get(cid, vol.get("percent", 100)))})
    return sorted(out, key=lambda r: r["label"].lower())


def set_room(cid: str, on: Optional[bool] = None,
             volume: Optional[int] = None, timeout: float = 2.0) -> dict:
    """Switch a room on or off and/or set its volume (0-100); the room's new
    row. ValueError for an id that is not a room on the music stream."""
    from .state import StateStore

    client = next((c for c in _clients(timeout) if str(c.get("id")) == cid), None)
    if client is None:
        raise ValueError(f"no room {cid!r} on the music stream")
    vol = (client.get("config") or {}).get("volume") or {}
    muted = bool(vol.get("muted"))
    state = StateStore()
    marker = state.get_rooms_duck()
    vols = dict((marker or {}).get("vols") or {})
    want = int(vols.get(cid, vol.get("percent", 100))) if volume is None \
        else max(0, min(100, int(volume)))
    plays = (not muted) if on is None else bool(on)
    sound = want
    if marker and plays:
        # Under a reply: the room is owed `want` when the duck lifts and
        # sits at the duck level until then.
        if vols.get(cid) != want:
            vols[cid] = want
            state.set_rooms_duck({**marker, "vols": vols})
        sound = min(want, int(marker.get("level") or 0))
    snapcast.set_client_volume(cid, sound, muted=None if on is None else not on,
                               timeout=timeout)
    return {"id": cid, "label": _label(client),
            "connected": bool(client.get("connected")), "on": plays, "volume": want}
