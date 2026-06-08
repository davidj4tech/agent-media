"""Gene Keys (Richard Rudd) as a thematic source.

Gene Keys share the same 64 gates/hexagrams as Human Design, so this source
needs no new astronomy — it joins on the gate numbers astrotunes already
computes and adds a richer vocabulary: each gate's Shadow → Gift → Siddhi
(its spectrum from reactive to creative to transcendent). We surface the keys
for the listener's core (incarnation-cross gates) and for the gates lit by
today's transits — pure flavour for the picker, no brief nudges.

Triads verified against genekeys.com / Richard Rudd, *The Gene Keys* (2013).
"""
from __future__ import annotations

from typing import Any

NAME = "genekeys"

# gate -> (Shadow, Gift, Siddhi)
GENE_KEYS: dict[int, tuple[str, str, str]] = {
    1: ("Entropy", "Freshness", "Beauty"),
    2: ("Dislocation", "Orientation", "Unity"),
    3: ("Chaos", "Innovation", "Innocence"),
    4: ("Intolerance", "Understanding", "Forgiveness"),
    5: ("Impatience", "Patience", "Timelessness"),
    6: ("Conflict", "Diplomacy", "Peace"),
    7: ("Division", "Guidance", "Virtue"),
    8: ("Mediocrity", "Style", "Exquisiteness"),
    9: ("Inertia", "Determination", "Invincibility"),
    10: ("Self-Obsession", "Naturalness", "Being"),
    11: ("Obscurity", "Idealism", "Light"),
    12: ("Vanity", "Discrimination", "Purity"),
    13: ("Discord", "Discernment", "Empathy"),
    14: ("Compromise", "Competence", "Bounteousness"),
    15: ("Dullness", "Magnetism", "Florescence"),
    16: ("Indifference", "Versatility", "Mastery"),
    17: ("Opinion", "Far-Sightedness", "Omniscience"),
    18: ("Judgement", "Integrity", "Perfection"),
    19: ("Co-Dependence", "Sensitivity", "Sacrifice"),
    20: ("Superficiality", "Self-Assurance", "Presence"),
    21: ("Control", "Authority", "Valour"),
    22: ("Dishonour", "Graciousness", "Grace"),
    23: ("Complexity", "Simplicity", "Quintessence"),
    24: ("Addiction", "Invention", "Silence"),
    25: ("Constriction", "Acceptance", "Universal Love"),
    26: ("Pride", "Artfulness", "Invisibility"),
    27: ("Selfishness", "Altruism", "Selflessness"),
    28: ("Purposelessness", "Totality", "Immortality"),
    29: ("Half-Heartedness", "Commitment", "Devotion"),
    30: ("Desire", "Lightness", "Rapture"),
    31: ("Arrogance", "Leadership", "Humility"),
    32: ("Failure", "Preservation", "Veneration"),
    33: ("Forgetting", "Mindfulness", "Revelation"),
    34: ("Force", "Strength", "Majesty"),
    35: ("Hunger", "Adventure", "Boundlessness"),
    36: ("Turbulence", "Humanity", "Compassion"),
    37: ("Weakness", "Equality", "Tenderness"),
    38: ("Struggle", "Perseverance", "Honour"),
    39: ("Provocation", "Dynamism", "Liberation"),
    40: ("Exhaustion", "Resolve", "Divine Will"),
    41: ("Fantasy", "Anticipation", "Emanation"),
    42: ("Expectation", "Detachment", "Celebration"),
    43: ("Deafness", "Insight", "Epiphany"),
    44: ("Interference", "Teamwork", "Synarchy"),
    45: ("Dominance", "Synergy", "Communion"),
    46: ("Seriousness", "Delight", "Ecstasy"),
    47: ("Oppression", "Transmutation", "Transfiguration"),
    48: ("Inadequacy", "Resourcefulness", "Wisdom"),
    49: ("Reaction", "Revolution", "Rebirth"),
    50: ("Corruption", "Equilibrium", "Harmony"),
    51: ("Agitation", "Initiative", "Awakening"),
    52: ("Stress", "Restraint", "Stillness"),
    53: ("Immaturity", "Expansion", "Superabundance"),
    54: ("Greed", "Aspiration", "Ascension"),
    55: ("Victimisation", "Freedom", "Freedom"),
    56: ("Distraction", "Enrichment", "Intoxication"),
    57: ("Unease", "Intuition", "Clarity"),
    58: ("Dissatisfaction", "Vitality", "Bliss"),
    59: ("Dishonesty", "Intimacy", "Transparency"),
    60: ("Limitation", "Realism", "Justice"),
    61: ("Psychosis", "Inspiration", "Sanctity"),
    62: ("Intellect", "Precision", "Impeccability"),
    63: ("Doubt", "Inquiry", "Truth"),
    64: ("Confusion", "Imagination", "Illumination"),
}


def _triad(gate: int) -> dict[str, Any]:
    s, g, d = GENE_KEYS[gate]
    return {"gate": gate, "shadow": s, "gift": g, "siddhi": d}


def contribute(ctx: dict[str, Any]) -> dict[str, Any]:
    hd = (ctx.get("natal") or {}).get("human_design") or {}
    if not hd:
        return {"available": False,
                "status": "needs human_design in context (kerykeion)"}

    core_gates: list[int] = []
    cross = (hd.get("incarnation_cross") or {}).get("gates") or {}
    for g in cross.values():
        if isinstance(g, int) and g not in core_gates:
            core_gates.append(g)

    transit_gates: list[int] = []
    overlay = ((ctx.get("transits") or {}).get("human_design") or {})
    for tg in overlay.get("transit_gates", []):
        g = tg.get("gate")
        if isinstance(g, int) and g not in transit_gates:
            transit_gates.append(g)

    core = [_triad(g) for g in core_gates if g in GENE_KEYS]
    today = [_triad(g) for g in transit_gates if g in GENE_KEYS][:6]

    # Gifts make the most useful flavour words for the picker.
    themes = sorted({t["gift"] for t in today} | {t["gift"] for t in core})

    return {
        "available": True,
        "status": "live",
        "summary": ("core gifts " + ", ".join(t["gift"] for t in core)
                    + (" · today " + ", ".join(t["gift"] for t in today) if today else "")),
        "themes": themes,
        "data": {"core": core, "today": today},
    }
