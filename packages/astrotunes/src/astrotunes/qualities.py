"""Deterministic translation layer: computed context -> musical quality tags.

This turns the *facts* astrotunes already computes (transits, Human Design,
weather, time of day, moon phase) into an explicit, tweakable brief — a set of
quality axes with a value, a label, and the reasons that moved it. The track
*pick* still happens downstream (the music-transit skill / a human), but the
brief it works from is now transparent instead of living only in the model's head.

Everything that decides a value lives in the CONFIG block below — adjust the
nudges there and the brief changes. Nothing here is astronomy; it's a stated set
of associations (warm Venus, austere Saturn, cosy rain…) that you can argue with.
"""
from __future__ import annotations

from typing import Any

# --- Axes -----------------------------------------------------------------
# Each axis runs 0.0 .. 1.0. (low_word, mid_word, high_word) name the ends.
AXES: dict[str, tuple[str, str, str]] = {
    "tempo":          ("slow / downtempo", "mid-tempo", "up-tempo"),
    "energy":         ("calm", "steady", "intense"),
    "warmth":         ("cool / austere", "neutral", "warm / intimate"),
    "brightness":     ("melancholic", "even", "uplifting"),
    "lyric_density":  ("instrumental", "some vocals", "vocal-forward"),
    "texture":        ("sparse / airy", "balanced", "dense / lush"),
}
BASE = 0.5  # every axis starts neutral, then gets nudged

# --- CONFIG (tweak me) ----------------------------------------------------
# All values are additive nudges to a 0.5 baseline, pre-clamp. Positive pushes
# toward the axis's high_word.

TIME_OF_DAY = {
    "late night":     {"tempo": -0.25, "energy": -0.25, "warmth": +0.10},
    "early morning":  {"tempo": -0.10, "energy": -0.10, "brightness": +0.05},
    "morning":        {"tempo": +0.05, "energy": +0.05, "brightness": +0.10},
    "midday":         {"tempo": +0.10, "energy": +0.10},
    "afternoon":      {"brightness": +0.05},
    "evening":        {"tempo": -0.05, "warmth": +0.10},
    "night":          {"tempo": -0.15, "energy": -0.15, "warmth": +0.10},
}

# Weather is matched by substring against the open-meteo label, plus temp/is_day.
WEATHER_LABEL = {
    "clear":     {"brightness": +0.10, "texture": -0.05},
    "cloud":     {"brightness": -0.05, "warmth": +0.05},
    "overcast":  {"brightness": -0.10, "warmth": +0.05, "texture": +0.05},
    "fog":       {"brightness": -0.10, "texture": +0.10, "lyric_density": -0.05},
    "drizzle":   {"warmth": +0.10, "texture": +0.10, "energy": -0.05},
    "rain":      {"warmth": +0.10, "texture": +0.10, "lyric_density": +0.05, "energy": -0.05},
    "shower":    {"warmth": +0.08, "texture": +0.08, "energy": -0.05},
    "snow":      {"warmth": +0.10, "texture": +0.10, "energy": -0.10},
    "thunder":   {"energy": +0.10, "texture": +0.10},
}
WEATHER_COLD_C = 12.0   # below this: cosy up
WEATHER_HOT_C = 28.0    # above this: open/airy
WEATHER_COLD = {"warmth": +0.15, "texture": +0.10}
WEATHER_HOT = {"texture": -0.15, "brightness": +0.05}
WEATHER_NIGHT = {"energy": -0.10}  # is_day == False

MOON_PHASE = {
    "new":             {"lyric_density": -0.10, "energy": -0.05},
    "waxing crescent": {"energy": +0.05, "brightness": +0.05},
    "first quarter":   {"energy": +0.05},
    "waxing gibbous":  {"energy": +0.05, "brightness": +0.05},
    "full":            {"energy": +0.10, "lyric_density": +0.05},
    "waning gibbous":  {"energy": -0.05, "brightness": -0.05},
    "last quarter":    {"energy": -0.05, "lyric_density": -0.05},
    "waning crescent": {"energy": -0.08, "brightness": -0.05, "lyric_density": -0.05},
}

# Transit→natal aspects. Each transiting planet has a "flavor" (axis nudges at
# unit strength); the aspect type scales it and adds its own character; the orb
# weights it (tighter = stronger).
PLANET_FLAVOR = {
    "sun":     {"brightness": +0.12, "energy": +0.06},
    "moon":    {"warmth": +0.10, "lyric_density": +0.08},
    "mercury": {"lyric_density": +0.10, "tempo": +0.04},
    "venus":   {"warmth": +0.12, "brightness": +0.08},
    "mars":    {"energy": +0.12, "tempo": +0.10},
    "jupiter": {"brightness": +0.10, "energy": +0.06, "texture": +0.05},
    "saturn":  {"energy": -0.10, "warmth": -0.08, "texture": -0.08, "brightness": -0.05},
    "uranus":  {"energy": +0.08, "texture": +0.06, "tempo": +0.05},
    "neptune": {"texture": +0.10, "lyric_density": -0.06, "brightness": -0.05},
    "pluto":   {"energy": +0.10, "brightness": -0.08},
}
ASPECT_MAX_ORB = 3.0   # ignore aspects looser than this
ASPECT_KIND = {        # (flavor_scale, extra nudges added regardless of planet)
    "trine":      (1.00, {"brightness": +0.05}),
    "sextile":    (0.90, {"brightness": +0.04}),
    "conjunction": (1.00, {}),                          # amplify the planet's flavor
    "square":     (0.70, {"energy": +0.05, "brightness": -0.05}),   # friction/edge
    "opposition": (0.70, {"energy": +0.04, "brightness": -0.04}),
}
# A dense natal chart yields many aspects; cap how far the whole aspect layer can
# move any one axis so it colors the brief without swamping time/weather/mood.
ASPECT_CAP = 0.20
# Same idea for the combined source layer (raga, HA, …).
SOURCE_CAP = 0.20

# Human Design: which axis each *open* center amplifies (sensitivity, not a value).
CENTER_AMPLIFIES = {
    "SolarPlexus": ["brightness"],   # emotional charge lands hard
    "Sacral":      ["tempo", "energy"],
    "Root":        ["energy", "tempo"],
    "Throat":      ["lyric_density"],
    "Head":        ["lyric_density"],
    "Ajna":        ["lyric_density"],
    "Heart":       ["energy"],
    "G":           ["warmth"],
    "Spleen":      [],               # -> spontaneity/novelty (posture, not an axis)
}
CENTER_DISPLAY_TO_KEY = {
    "Head": "Head", "Ajna": "Ajna", "Throat": "Throat", "G (Identity)": "G",
    "Heart (Ego/Will)": "Heart", "Solar Plexus (Emotional)": "SolarPlexus",
    "Sacral": "Sacral", "Spleen": "Spleen", "Root": "Root",
}
TYPE_POSTURE = {
    "Reflector": "Sampler — favour variety; the field sets the tone, so let it choose.",
    "Projector": "Quality over quantity; focused, with room to rest.",
    "Generator": "Build and sustain a satisfying groove; follow what lights up.",
    "Manifesting Generator": "Groove that can pivot; follow the spark, keep momentum.",
    "Manifestor": "Self-contained, initiating energy.",
}

# Magnitude below which a contribution isn't worth listing as a reason.
REASON_EPS = 0.03


def _add(values: dict[str, float], reasons: dict[str, list[str]],
         nudges: dict[str, float], source: str, weight: float = 1.0) -> None:
    for axis, delta in nudges.items():
        d = delta * weight
        if axis not in values:
            continue
        values[axis] += d
        if abs(d) >= REASON_EPS:
            arrow = "↑" if d > 0 else "↓"
            reasons[axis].append(f"{source} {arrow}{abs(d):.2f}")


def _label(axis: str, v: float) -> str:
    lo, mid, hi = AXES[axis]
    if v < 0.34:
        return lo
    if v > 0.66:
        return hi
    return mid


def derive(ctx: dict[str, Any]) -> dict[str, Any]:
    """Build the musical-qualities brief from an assembled astrotunes context.

    Reads ctx["time_of_day"], ctx["transits"] (aspects_to_natal, moon, HD overlay),
    ctx["weather"], and ctx["natal"]["human_design"]. Missing pieces are skipped
    gracefully — the brief degrades rather than failing.
    """
    values = {a: BASE for a in AXES}
    reasons: dict[str, list[str]] = {a: [] for a in AXES}

    # 1. Time of day
    tod = ctx.get("time_of_day")
    if tod in TIME_OF_DAY:
        _add(values, reasons, TIME_OF_DAY[tod], f"time:{tod}")

    transits = ctx.get("transits") or {}

    # 2. Moon phase
    phase = transits.get("moon_phase_label")
    if phase in MOON_PHASE:
        _add(values, reasons, MOON_PHASE[phase], f"moon:{phase}")

    # 3. Weather
    w = ctx.get("weather") or {}
    label = (w.get("label") or "").lower()
    for key, nudges in WEATHER_LABEL.items():
        if key in label:
            _add(values, reasons, nudges, f"weather:{label}")
            break
    temp = w.get("temperature_c")
    if isinstance(temp, (int, float)):
        if temp < WEATHER_COLD_C:
            _add(values, reasons, WEATHER_COLD, f"weather:{temp:g}°C cold")
        elif temp > WEATHER_HOT_C:
            _add(values, reasons, WEATHER_HOT, f"weather:{temp:g}°C hot")
    if w.get("is_day") is False:
        _add(values, reasons, WEATHER_NIGHT, "weather:dark")

    # 4. Transit→natal aspects (the astrology energy/tone). Accumulate into a
    # separate layer, cap it per axis, then fold into the brief — so a dense
    # chart's many aspects color without saturating.
    asp_values: dict[str, float] = {a: 0.0 for a in AXES}
    asp_reasons: dict[str, list[str]] = {a: [] for a in AXES}
    for asp in transits.get("aspects_to_natal", []):
        orb = asp.get("orb", 99)
        if orb > ASPECT_MAX_ORB:
            continue
        planet = asp.get("transit")
        kind = asp.get("aspect")
        flavor = PLANET_FLAVOR.get(planet)
        scale_extra = ASPECT_KIND.get(kind)
        if not flavor or not scale_extra:
            continue
        scale, extra = scale_extra
        w_orb = (ASPECT_MAX_ORB - orb) / ASPECT_MAX_ORB  # 0..1, tighter = bigger
        src = f"{planet} {kind} natal-{asp.get('natal')}"
        _add(asp_values, asp_reasons, flavor, src, weight=scale * w_orb)
        if extra:
            _add(asp_values, asp_reasons, extra, f"{kind} edge ({planet})", weight=w_orb)

    for a in AXES:
        capped = max(-ASPECT_CAP, min(ASPECT_CAP, asp_values[a]))
        values[a] += capped
        reasons[a].extend(asp_reasons[a])
        if abs(asp_values[a]) > ASPECT_CAP:
            reasons[a].append(f"aspects net {asp_values[a]:+.2f} → capped at ±{ASPECT_CAP:.2f}")

    # 4b. Source nudges (raga rasa, HA heart-rate, …). Capped as one layer too,
    # so a chatty source colours the brief without dominating it.
    from .sources import collected_nudges
    src_nudges = collected_nudges(ctx.get("sources") or {})
    for axis, delta in src_nudges.items():
        if axis not in values:
            continue
        capped = max(-SOURCE_CAP, min(SOURCE_CAP, delta))
        values[axis] += capped
        if abs(capped) >= REASON_EPS:
            arrow = "↑" if capped > 0 else "↓"
            reasons[axis].append(f"sources {arrow}{abs(capped):.2f}")

    # Clamp
    for a in values:
        values[a] = max(0.0, min(1.0, values[a]))

    # 5. Human Design overlay — amplified axes (sensitivity) + posture + themes
    hd = (ctx.get("natal") or {}).get("human_design") or {}
    amplified: list[str] = []
    for disp in hd.get("undefined_centers", []):
        key = CENTER_DISPLAY_TO_KEY.get(disp)
        for axis in CENTER_AMPLIFIES.get(key, []):
            if axis not in amplified:
                amplified.append(axis)
    spleen_open = "Spleen" in [CENTER_DISPLAY_TO_KEY.get(d) for d in hd.get("undefined_centers", [])]
    posture = TYPE_POSTURE.get(hd.get("type"), "")

    hd_overlay = (transits.get("human_design") or {})
    themes = [c["name"] for c in hd_overlay.get("channels_activated", [])]

    brief = {
        a: {
            "value": round(values[a], 2),
            "label": _label(a, values[a]),
            "amplified": a in amplified,
            "reasons": reasons[a],
        }
        for a in AXES
    }

    return {
        "axes": brief,
        "amplified_axes": amplified,
        "posture": posture,
        "novelty": spleen_open or hd.get("type") == "Reflector",
        "themes": themes,
        "note": (
            "Deterministic brief from the computed context. 'amplified' axes are "
            "open Human Design centers — the listener feels those dimensions most "
            "acutely, so pick deliberately there. Mood + activity (asked live) "
            "override this. Track selection is downstream judgment, not in here."
        ),
    }
