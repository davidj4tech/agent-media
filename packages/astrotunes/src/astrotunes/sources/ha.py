"""Home Assistant biometric/presence context — config-gated.

The rigorous version of "how are you feeling": pull a live signal from Home
Assistant — heart rate / HRV from a wearable, or which room you're in — and let
real physiological energy nudge tempo/energy instead of (or alongside) a guess.

Dormant until you set env vars:
  HA_URL         — e.g. http://homeassistant.local:8123  (or the Tailscale host)
  HA_TOKEN       — a long-lived access token (HA → profile → security)
  HA_HR_ENTITY   — optional, the heart-rate sensor entity id
                   (e.g. sensor.pixel_watch_heart_rate)

When live it maps resting-ish vs elevated HR onto the energy/tempo axes. No deps
beyond httpx.
"""
from __future__ import annotations

import os
from typing import Any

NAME = "ha"

# Rough HR → energy nudge anchors (bpm). Tweak to your own resting/active range.
HR_REST = 60.0
HR_ACTIVE = 110.0


def contribute(ctx: dict[str, Any]) -> dict[str, Any]:
    url = os.environ.get("HA_URL")
    token = os.environ.get("HA_TOKEN")
    entity = os.environ.get("HA_HR_ENTITY")
    if not (url and token):
        return {"available": False,
                "status": "configure: set HA_URL + HA_TOKEN (+ HA_HR_ENTITY) "
                          "for live heart-rate/presence context"}
    if not entity:
        return {"available": False,
                "status": "configure: set HA_HR_ENTITY to a heart-rate sensor"}

    try:
        import httpx
        r = httpx.get(f"{url.rstrip('/')}/api/states/{entity}",
                      headers={"Authorization": f"Bearer {token}"}, timeout=5)
        r.raise_for_status()
        hr = float(r.json().get("state"))
    except Exception as e:
        return {"available": False, "status": f"HA request failed: {e}"}

    # Map HR into a -1..+1 arousal scale, then onto energy/tempo nudges.
    frac = (hr - HR_REST) / (HR_ACTIVE - HR_REST)
    arousal = max(-1.0, min(1.0, (frac - 0.5) * 2.0))
    nudges = {"energy": round(0.20 * arousal, 3), "tempo": round(0.12 * arousal, 3)}

    state = "elevated" if arousal > 0.2 else "calm" if arousal < -0.2 else "neutral"
    return {
        "available": True,
        "status": "live",
        "summary": f"heart rate {hr:g} bpm → {state}",
        "brief_nudges": nudges,
        "data": {"heart_rate_bpm": hr, "arousal": round(arousal, 2)},
    }
