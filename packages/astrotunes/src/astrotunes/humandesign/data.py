"""Fixed Human Design reference constants.

These are the static tables of the system — the Rave Mandala wheel offset, the
64-gate zodiacal order, the gate→center map, the 36 channels, and the gate
keynotes. They are not configuration; they are the definition of the bodygraph.
Verified against Jovian Archive (the official Ra Uru Hu lineage) and corroborating
gate-by-degree tables. See README for sources.
"""
from __future__ import annotations

# --- Wheel geometry -------------------------------------------------------
# Gate 41 begins at 2°00' Aquarius = 302.0° absolute tropical longitude.
# Each gate spans 360/64 = 5.625°; each of its 6 lines spans 0.9375°.
WHEEL_START = 302.0
GATE_ARC = 5.625
LINE_ARC = 0.9375

# 64 gates in zodiacal order (increasing longitude) from the wheel's zero point.
GATE_ORDER = [
    41, 19, 13, 49, 30, 55, 37, 63, 22, 36, 25, 17, 21, 51, 42, 3,
    27, 24, 2, 23, 8, 20, 16, 35, 45, 12, 15, 52, 39, 53, 62, 56,
    31, 33, 7, 4, 29, 59, 40, 64, 47, 6, 46, 18, 48, 57, 32, 50,
    28, 44, 1, 43, 14, 34, 9, 5, 26, 11, 10, 58, 38, 54, 61, 60,
]
assert len(GATE_ORDER) == 64 and len(set(GATE_ORDER)) == 64

# --- Centers --------------------------------------------------------------
# Canonical center keys used throughout the package.
CENTERS = [
    "Head", "Ajna", "Throat", "G", "Heart",
    "SolarPlexus", "Sacral", "Spleen", "Root",
]

CENTER_GATES: dict[str, list[int]] = {
    "Head": [61, 63, 64],
    "Ajna": [4, 11, 17, 24, 43, 47],
    "Throat": [8, 12, 16, 20, 23, 31, 33, 35, 45, 56, 62],
    "G": [1, 2, 7, 10, 13, 15, 25, 46],
    "Heart": [21, 26, 40, 51],
    "SolarPlexus": [6, 22, 30, 36, 37, 49, 55],
    "Sacral": [3, 5, 9, 14, 27, 29, 34, 42, 59],
    "Spleen": [18, 28, 32, 44, 48, 50, 57],
    "Root": [19, 38, 39, 41, 52, 53, 54, 58, 60],
}

# Reverse: gate -> center.
GATE_CENTER: dict[int, str] = {
    g: center for center, gates in CENTER_GATES.items() for g in gates
}
assert len(GATE_CENTER) == 64

# The four motor centers (sources of consistent energy / drive).
MOTORS = {"Sacral", "SolarPlexus", "Heart", "Root"}

# Human-friendly display names.
CENTER_DISPLAY = {
    "Head": "Head", "Ajna": "Ajna", "Throat": "Throat", "G": "G (Identity)",
    "Heart": "Heart (Ego/Will)", "SolarPlexus": "Solar Plexus (Emotional)",
    "Sacral": "Sacral", "Spleen": "Spleen", "Root": "Root",
}

# --- Channels (36) --------------------------------------------------------
# (gate_a, gate_b), name, (center_a, center_b)
CHANNELS: list[tuple[tuple[int, int], str, tuple[str, str]]] = [
    ((1, 8),   "Inspiration",    ("G", "Throat")),
    ((2, 14),  "The Beat",       ("G", "Sacral")),
    ((3, 60),  "Mutation",       ("Sacral", "Root")),
    ((4, 63),  "Logic",          ("Head", "Ajna")),
    ((5, 15),  "Rhythm",         ("Sacral", "G")),
    ((6, 59),  "Mating",         ("Sacral", "SolarPlexus")),
    ((7, 31),  "The Alpha",      ("G", "Throat")),
    ((9, 52),  "Concentration",  ("Sacral", "Root")),
    ((10, 20), "Awakening",      ("G", "Throat")),
    ((10, 34), "Exploration",    ("G", "Sacral")),
    ((10, 57), "Perfected Form", ("G", "Spleen")),
    ((11, 56), "Curiosity",      ("Ajna", "Throat")),
    ((12, 22), "Openness",       ("Throat", "SolarPlexus")),
    ((13, 33), "The Prodigal",   ("G", "Throat")),
    ((16, 48), "The Wavelength", ("Spleen", "Throat")),
    ((17, 62), "Acceptance",     ("Ajna", "Throat")),
    ((18, 58), "Judgment",       ("Spleen", "Root")),
    ((19, 49), "Synthesis",      ("Root", "SolarPlexus")),
    ((20, 34), "Charisma",       ("Throat", "Sacral")),
    ((20, 57), "The Brainwave",  ("Throat", "Spleen")),
    ((21, 45), "Money Line",     ("Heart", "Throat")),
    ((23, 43), "Structuring",    ("Ajna", "Throat")),
    ((24, 61), "Awareness",      ("Head", "Ajna")),
    ((25, 51), "Initiation",     ("G", "Heart")),
    ((26, 44), "Surrender",      ("Spleen", "Heart")),
    ((27, 50), "Preservation",   ("Sacral", "Spleen")),
    ((28, 38), "Struggle",       ("Spleen", "Root")),
    ((29, 46), "Discovery",      ("Sacral", "G")),
    ((30, 41), "Recognition",    ("SolarPlexus", "Root")),
    ((32, 54), "Transformation", ("Spleen", "Root")),
    ((34, 57), "Power",          ("Sacral", "Spleen")),
    ((35, 36), "Transitoriness", ("Throat", "SolarPlexus")),
    ((37, 40), "Community",      ("SolarPlexus", "Heart")),
    ((39, 55), "Emoting",        ("Root", "SolarPlexus")),
    ((42, 53), "Maturation",     ("Sacral", "Root")),
    ((47, 64), "Abstraction",    ("Head", "Ajna")),
]
assert len(CHANNELS) == 36

# --- Type / Strategy / Authority prose -----------------------------------
STRATEGY = {
    "Manifestor": "Inform before you act",
    "Generator": "Wait to respond",
    "Manifesting Generator": "Respond, then inform",
    "Projector": "Wait for the invitation",
    "Reflector": "Wait a lunar cycle",
}

SIGNATURE = {
    "Manifestor": "Peace",
    "Generator": "Satisfaction",
    "Manifesting Generator": "Satisfaction",
    "Projector": "Success",
    "Reflector": "Surprise",
}

NOT_SELF = {
    "Manifestor": "Anger",
    "Generator": "Frustration",
    "Manifesting Generator": "Frustration",
    "Projector": "Bitterness",
    "Reflector": "Disappointment",
}

AUTHORITY_NOTE = {
    "Emotional": "Wait out the emotional wave — no truth in the now",
    "Sacral": "Trust the gut response in the moment (uh-huh / uh-uh)",
    "Splenic": "Trust the quiet, one-time intuitive hit",
    "Ego": "Follow what the heart truly wants / can commit to",
    "Self-Projected": "Talk it out — listen to your own voice",
    "Mental": "No inner authority — talk to trusted sounding-boards, sleep on it",
    "Lunar": "Wait ~28 days; let clarity cycle in",
}

# --- Profile lines --------------------------------------------------------
PROFILE_LINE = {
    1: "Investigator",
    2: "Hermit",
    3: "Martyr",
    4: "Opportunist",
    5: "Heretic",
    6: "Role Model",
}

# Profile -> incarnation cross angle.
RIGHT_ANGLE = {(1, 3), (1, 4), (2, 4), (2, 5), (3, 5), (3, 6), (4, 6)}
JUXTAPOSITION = {(4, 1)}
LEFT_ANGLE = {(5, 1), (5, 2), (6, 2), (6, 3)}

# --- Gate keynotes (I Ching theme / HD keynote) --------------------------
GATE_KEYNOTE: dict[int, str] = {
    1: "Self-expression / the Creative",
    2: "Direction of the self / the Receptive",
    3: "Ordering / mutation, new beginnings",
    4: "Formulization / mental answers",
    5: "Fixed rhythms / waiting, patterns",
    6: "Friction / intimacy & emotional conflict",
    7: "Role of the self / leadership through service",
    8: "Contribution / making a difference",
    9: "Focus / concentration on detail",
    10: "Behavior of the self / love of self",
    11: "Ideas / harmony of concepts",
    12: "Caution / standstill, articulation",
    13: "The listener / fellowship, keeper of secrets",
    14: "Power skills / drive, resource generation",
    15: "Extremes / modesty, love of humanity",
    16: "Skills / enthusiasm, talent",
    17: "Opinions / following (logic)",
    18: "Correction / judgment, perfectionism",
    19: "Wanting / approach, sensitivity & needs",
    20: "The now / contemplation, present awareness",
    21: "The hunter / control of the material",
    22: "Openness / grace (emotional)",
    23: "Assimilation / individual knowing",
    24: "Rationalization / returning, review",
    25: "Spirit of the self / innocence, universal love",
    26: "The egoist / the trickster, salesmanship",
    27: "Caring / nourishment",
    28: "The game player / struggle, finding purpose",
    29: "Perseverance / saying yes, commitment",
    30: "Recognition of feelings / desire, the clinging fire",
    31: "Leading / democratic influence",
    32: "Continuity / duration, instinct for endurance",
    33: "Privacy / retreat, memory & telling",
    34: "Power / great power, life force",
    35: "Change / progress, experience",
    36: "Crisis / darkening of the light, new experiences",
    37: "Friendship / the family, community",
    38: "The fighter / opposition, fighting for purpose",
    39: "Provocation / obstruction, the provocateur",
    40: "Aloneness / deliverance, willpower & rest",
    41: "Contraction / decrease, fantasy & imagination",
    42: "Growth / increase, completion of cycles",
    43: "Insight / breakthrough, individual knowing",
    44: "Alertness / coming to meet, instinct",
    45: "The gatherer / the king/queen, resources",
    46: "Determination of the self / love of the body",
    47: "Realization / oppression, mental pressure",
    48: "Depth / the well, mastery",
    49: "Principles / revolution, ideals",
    50: "Values / the cauldron, tribal laws",
    51: "Shock / the arousing, initiation & courage",
    52: "Stillness / keeping still, concentration",
    53: "Beginnings / development, starting things",
    54: "Ambition / drive to rise",
    55: "Spirit / abundance, emotional fullness",
    56: "Stimulation / the wanderer, storytelling",
    57: "Intuitive clarity / the gentle, intuition",
    58: "Vitality / the joyous, joy of improvement",
    59: "Sexuality / dispersion, intimacy",
    60: "Acceptance / limitation, mutation through constraint",
    61: "Inner truth / mystery, inspiration",
    62: "Detail / preponderance of the small, precision",
    63: "Doubt / after completion, logical questioning",
    64: "Confusion / before completion, abstract pressure",
}
assert len(GATE_KEYNOTE) == 64

# The 13 bodies (besides Earth & South Node, which are derived) we read off a
# kerykeion subject. Order matters only for stable output.
KERYKEION_BODIES = [
    "sun", "moon", "mercury", "venus", "mars", "jupiter",
    "saturn", "uranus", "neptune", "pluto",
]
# Kerykeion v5 north-node attribute (with a v4 fallback handled in code).
NORTH_NODE_ATTR = "true_north_lunar_node"
