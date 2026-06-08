"""Raga samay — Hindustani raga time-theory as a music source.

Raga samay assigns ragas to times of day (the eight prahars) and some to
seasons — a centuries-old "music for the moment" system, the closest historical
cousin to what astrotunes does. This source matches the current time-of-day (and
Melbourne's *southern-hemisphere* season) to the traditional ragas, contributes
their rasa (mood) to the qualities brief, and offers canonical recordings as
concrete search seeds.

Time placements follow the standard Bhatkhande thaat–time consensus. Raga time
theory is a tradition with regional variation, not a hard standard — treat it as
flavour, not law. Verify a specific recording before pinning a URL.
"""
from __future__ import annotations

from typing import Any

NAME = "raga"

# raga -> (time_bucket, season, rasa_words, [seed_artists])
# season: "" | "spring" | "monsoon"
RAGAS: dict[str, tuple[str, str, list[str], list[str]]] = {
    "Bhairav":            ("dawn", "", ["serene", "devotional", "austere", "majestic"], ["Bhimsen Joshi", "Ravi Shankar"]),
    "Ramkali":            ("dawn", "", ["devotional", "serious", "ascetic"], ["Bhimsen Joshi", "Mallikarjun Mansur"]),
    "Lalit":              ("dawn", "", ["melancholic", "yearning"], ["Nikhil Banerjee", "Kishori Amonkar"]),
    "Ahir Bhairav":       ("dawn", "", ["devotional", "gentle", "hopeful"], ["Nikhil Banerjee", "Ali Akbar Khan"]),
    "Todi":               ("morning", "", ["plaintive", "longing", "serene"], ["Bhimsen Joshi", "Nikhil Banerjee"]),
    "Bilaskhani Todi":    ("morning", "", ["pathos", "mournful", "tender"], ["Kishori Amonkar", "Amir Khan"]),
    "Deshkar":            ("morning", "", ["bright", "expansive", "joyful"], ["Kishori Amonkar", "Mogubai Kurdikar"]),
    "Bilawal":            ("morning", "", ["bright", "cheerful", "settled"], ["Bhimsen Joshi", "Vilayat Khan"]),
    "Jaunpuri":           ("late_morning", "", ["serene", "contemplative"], ["Kesarbai Kerkar", "Amir Khan"]),
    "Brindabani Sarang":  ("midday", "", ["bright", "refreshing", "open"], ["Bhimsen Joshi", "Ravi Shankar"]),
    "Shuddh Sarang":      ("midday", "", ["energetic", "luminous"], ["Rashid Khan", "Nikhil Banerjee"]),
    "Multani":            ("afternoon", "", ["intense", "plaintive", "serious"], ["Bhimsen Joshi", "Amir Khan"]),
    "Bhimpalasi":         ("afternoon", "", ["romantic", "tender", "longing", "sweet"], ["Nikhil Banerjee", "Vilayat Khan"]),
    "Madhuvanti":         ("afternoon", "", ["romantic", "yearning", "sweet"], ["Nikhil Banerjee", "Shruti Sadolikar"]),
    "Marwa":              ("dusk", "", ["solemn", "abstract", "unsettled"], ["Nikhil Banerjee", "Amir Khan"]),
    "Puriya":             ("dusk", "", ["introspective", "intense", "devotional"], ["Nikhil Banerjee", "Kishori Amonkar"]),
    "Puriya Dhanashree":  ("dusk", "", ["devotional", "intense", "pathos"], ["Bhimsen Joshi", "Kishori Amonkar"]),
    "Shree":              ("dusk", "", ["grave", "solemn", "devotional"], ["Bhimsen Joshi", "Amir Khan"]),
    "Yaman":              ("early_night", "", ["serene", "romantic", "devotional", "uplifting"], ["Nikhil Banerjee", "Bhimsen Joshi"]),
    "Bhupali":            ("early_night", "", ["serene", "devotional", "peaceful"], ["Kishori Amonkar", "Bhimsen Joshi"]),
    "Hamir":              ("early_night", "", ["heroic", "grand", "joyful"], ["Vilayat Khan", "Rashid Khan"]),
    "Kedar":              ("early_night", "", ["devotional", "romantic", "majestic"], ["Bhimsen Joshi", "Vilayat Khan"]),
    "Bihag":              ("night", "", ["romantic", "sweet", "tender"], ["Bhimsen Joshi", "Vilayat Khan"]),
    "Khamaj":             ("night", "", ["romantic", "light", "playful"], ["Vilayat Khan", "Nikhil Banerjee"]),
    "Jaijaivanti":        ("night", "", ["tender", "devotional", "contemplative"], ["Bhimsen Joshi", "Nikhil Banerjee"]),
    "Bageshri":           ("night", "", ["romantic", "longing"], ["Nikhil Banerjee", "Kishori Amonkar"]),
    "Rageshri":           ("night", "", ["serene", "devotional", "sweet"], ["Ali Akbar Khan", "Nikhil Banerjee"]),
    "Darbari Kanada":     ("late_night", "", ["grave", "majestic", "profound", "serious"], ["Amir Khan", "Bhimsen Joshi"]),
    "Malkauns":           ("late_night", "", ["meditative", "intense", "ascetic", "dark"], ["Nikhil Banerjee", "Amir Khan"]),
    "Adana":              ("late_night", "", ["heroic", "energetic", "serious"], ["Amir Khan", "Rashid Khan"]),
    "Chandrakauns":       ("late_night", "", ["meditative", "mystical"], ["Nikhil Banerjee", "Hariprasad Chaurasia"]),
    "Sohini":             ("late_night", "", ["yearning", "romantic", "plaintive"], ["Kishori Amonkar", "Nikhil Banerjee"]),
    # seasonal (override time when in season)
    "Basant":             ("late_night", "spring", ["joyful", "festive", "romantic"], ["Bhimsen Joshi", "Kishori Amonkar"]),
    "Bahar":              ("night", "spring", ["joyful", "lively", "exuberant"], ["Bhimsen Joshi", "Vilayat Khan"]),
    "Hindol":             ("morning", "spring", ["bright", "joyful"], ["Ravi Shankar", "Nikhil Banerjee"]),
    "Miyan ki Malhar":    ("night", "monsoon", ["majestic", "yearning"], ["Amir Khan", "Bhimsen Joshi"]),
    "Megh Malhar":        ("night", "monsoon", ["heroic", "evocative"], ["Bhimsen Joshi", "Ravi Shankar"]),
}
# Bhairavi is the traditional "concluding raga, any time" wildcard.
WILDCARD = ("Bhairavi", ["devotional", "tender", "all-encompassing"], ["Bhimsen Joshi", "Hariprasad Chaurasia"])

# astrotunes time_of_day -> raga buckets (ordered preference)
TOD_TO_BUCKETS = {
    "late night":    ["late_night", "dawn"],
    "early morning": ["dawn", "morning"],
    "morning":       ["morning", "late_morning"],
    "midday":        ["midday", "late_morning"],
    "afternoon":     ["afternoon"],
    "evening":       ["dusk", "early_night"],
    "night":         ["early_night", "night"],
}

# rasa word -> brief nudges (averaged across selected ragas, then capped)
RASA_NUDGE = {
    "serene": {"energy": -0.10, "warmth": +0.05},
    "peaceful": {"energy": -0.10, "warmth": +0.05},
    "meditative": {"energy": -0.12, "texture": +0.05},
    "devotional": {"warmth": +0.08, "brightness": +0.05},
    "romantic": {"warmth": +0.10, "lyric_density": +0.05},
    "tender": {"warmth": +0.08},
    "sweet": {"warmth": +0.06, "brightness": +0.04},
    "longing": {"brightness": -0.06, "warmth": +0.05},
    "yearning": {"brightness": -0.06, "warmth": +0.05},
    "melancholic": {"brightness": -0.10, "warmth": +0.05},
    "plaintive": {"brightness": -0.08},
    "pathos": {"brightness": -0.08},
    "mournful": {"brightness": -0.10},
    "heroic": {"energy": +0.10},
    "grand": {"energy": +0.08, "texture": +0.05},
    "majestic": {"energy": +0.08},
    "joyful": {"brightness": +0.12, "energy": +0.08},
    "bright": {"brightness": +0.10},
    "festive": {"brightness": +0.10, "energy": +0.08},
    "lively": {"energy": +0.10, "tempo": +0.06},
    "exuberant": {"energy": +0.10, "tempo": +0.06},
    "cheerful": {"brightness": +0.08},
    "playful": {"brightness": +0.06, "tempo": +0.04},
    "austere": {"brightness": -0.06, "texture": -0.05},
    "ascetic": {"brightness": -0.06, "texture": -0.06},
    "grave": {"brightness": -0.08, "energy": -0.04},
    "serious": {"brightness": -0.05},
    "dark": {"brightness": -0.10},
    "intense": {"energy": +0.08},
    "energetic": {"energy": +0.10, "tempo": +0.06},
    "luminous": {"brightness": +0.08},
    "refreshing": {"brightness": +0.06, "texture": -0.04},
    "uplifting": {"brightness": +0.10},
    "contemplative": {"energy": -0.06},
    "introspective": {"energy": -0.06, "brightness": -0.04},
}
MAX_PICKS = 3


def _season_melbourne(month: int) -> str:
    # Southern hemisphere: spring Sep–Nov, summer Dec–Feb, autumn Mar–May, winter Jun–Aug.
    if month in (9, 10, 11):
        return "spring"
    if month in (6, 7, 8):
        return "winter"
    if month in (3, 4, 5):
        return "autumn"
    return "summer"


def contribute(ctx: dict[str, Any]) -> dict[str, Any]:
    tod = ctx.get("time_of_day")
    buckets = TOD_TO_BUCKETS.get(tod, [])
    now = ctx.get("now", "")
    month = int(now[5:7]) if len(now) >= 7 and now[5:7].isdigit() else 0
    season = _season_melbourne(month)

    picks: list[tuple[str, list[str], list[str]]] = []
    # seasonal ragas first if in season (spring/monsoon — monsoon rare in Melbourne)
    for name, (bkt, ssn, rasa, artists) in RAGAS.items():
        if ssn and ssn == season:
            picks.append((name, rasa, artists))
    # then time-matched
    for bkt in buckets:
        for name, (rb, ssn, rasa, artists) in RAGAS.items():
            if rb == bkt and not ssn and (name, rasa, artists) not in picks:
                picks.append((name, rasa, artists))
    if not picks:  # fall back to the any-time wildcard
        picks.append(WILDCARD)
    picks = picks[:MAX_PICKS]

    # Average the rasa nudges across picks.
    nudge_acc: dict[str, list[float]] = {}
    rasa_words: list[str] = []
    for _, rasa, _ in picks:
        for word in rasa:
            rasa_words.append(word)
            for axis, d in RASA_NUDGE.get(word, {}).items():
                nudge_acc.setdefault(axis, []).append(d)
    brief_nudges = {a: round(sum(v) / len(v), 3) for a, v in nudge_acc.items()}

    seeds = [
        {"query": f"Raga {name} {artists[0]}",
         "why": f"{tod} raga, {'/'.join(rasa[:2])}"}
        for name, rasa, artists in picks
    ]
    themes = sorted(set(rasa_words))

    names = ", ".join(p[0] for p in picks)
    return {
        "available": True,
        "status": "live",
        "summary": f"{tod} ragas ({season} in Melbourne): {names}",
        "brief_nudges": brief_nudges,
        "themes": themes,
        "seeds": seeds,
        "data": {"season_melbourne": season,
                 "ragas": [{"name": n, "rasa": r, "artists": a} for n, r, a in picks]},
    }
