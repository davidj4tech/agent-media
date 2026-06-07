"""Human Design bodygraph computed from birth data.

The static bodygraph (Type, Strategy, Authority, Profile, defined/undefined
Centers, channels, incarnation cross) is derived the same way the natal chart
and transits are: from birth data via kerykeion. Human Design reads two charts —
the *Personality* (the birth moment) and the *Design* (the moment the Sun was
88° of arc earlier, ~88 days before birth) — and maps each body's ecliptic
longitude onto the 64-gate I Ching wheel. A gate is *activated* if any of the 26
placements (13 bodies × 2 charts) falls in it; a channel is *defined* when both
its gates are activated; a center is *defined* when a channel touching it is.

This module exposes:
  - compute_bodygraph(natal) -> Bodygraph   (the static chart)
  - Bodygraph.transit_overlay(when)         (today's dynamic gate activations)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import data as D
from ..natal.sacred import NatalData

try:
    from kerykeion import AstrologicalSubject
    _KERYKEION_AVAILABLE = True
except ImportError:
    AstrologicalSubject = None  # type: ignore
    _KERYKEION_AVAILABLE = False


# --- Wheel mapping --------------------------------------------------------

def gate_line(longitude: float) -> tuple[int, int]:
    """Map an ecliptic longitude (0-360) to its Human Design gate and line."""
    x = (longitude - D.WHEEL_START) % 360.0
    idx = int(x / D.GATE_ARC)
    gate = D.GATE_ORDER[idx]
    within = x - idx * D.GATE_ARC
    line = int(within / D.LINE_ARC) + 1
    return gate, min(line, 6)


# --- kerykeion subject helpers -------------------------------------------

def _online_natal_subject(natal: NatalData) -> Any:
    """Build the birth subject online (geocodes the city) so we can read coords."""
    return AstrologicalSubject(
        "Natal", year=natal.year, month=natal.month, day=natal.day,
        hour=natal.hour, minute=natal.minute,
        city=natal.city, nation=natal.nation, tz_str=natal.timezone,
    )


def _offline_subject(when: datetime, lng: float, lat: float, tz: str, city: str) -> Any:
    """Build a subject from explicit coordinates — no geonames lookup."""
    return AstrologicalSubject(
        "X", year=when.year, month=when.month, day=when.day,
        hour=when.hour, minute=when.minute,
        lng=lng, lat=lat, tz_str=tz, city=city, online=False,
    )


def _north_node(subject: Any) -> Any:
    """Lunar north node, tolerating the kerykeion v4→v5 attribute rename."""
    for attr in (D.NORTH_NODE_ATTR, "true_node", "mean_north_lunar_node", "mean_node"):
        body = getattr(subject, attr, None)
        if body is not None:
            return body
    raise RuntimeError("kerykeion subject exposes no lunar node attribute")


def _activations(subject: Any) -> dict[str, dict[str, Any]]:
    """All 13 HD bodies for one chart, as {body: {gate, line, longitude}}.

    Earth is opposite the Sun; the South Node is opposite the North Node.
    """
    out: dict[str, dict[str, Any]] = {}

    def place(name: str, longitude: float) -> None:
        g, ln = gate_line(longitude)
        out[name] = {"gate": g, "line": ln, "longitude": round(longitude % 360, 3)}

    for body in D.KERYKEION_BODIES:
        place(body, getattr(subject, body).abs_pos)
    sun_long = getattr(subject, "sun").abs_pos
    place("earth", sun_long + 180.0)
    nn_long = _north_node(subject).abs_pos
    place("north_node", nn_long)
    place("south_node", nn_long + 180.0)
    return out


# --- Design (88° solar arc) solve ----------------------------------------

def _design_datetime(birth_dt: datetime, natal_sun_long: float,
                     lng: float, lat: float, tz: str, city: str) -> datetime:
    """Find the moment ~88 days before birth when the Sun was 88° of arc behind.

    f(d) = arc the Sun travelled backward in d days, a value that climbs roughly
    linearly from 0; it is monotonic over the search window so we bisect for 88°.
    """
    def arc_back(days: float) -> float:
        cand = birth_dt - timedelta(days=days)
        s = _offline_subject(cand, lng, lat, tz, city)
        return (natal_sun_long - s.sun.abs_pos) % 360.0

    lo, hi = 80.0, 95.0  # 88° at ~0.95–1.02°/day ⇒ 86–93 days; bracket safely
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if arc_back(mid) < 88.0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-5:
            break
    return birth_dt - timedelta(days=(lo + hi) / 2.0)


# --- Bodygraph derivation -------------------------------------------------

def _defined_channels(activated: set[int]) -> list[dict[str, Any]]:
    out = []
    for (g1, g2), name, (c1, c2) in D.CHANNELS:
        if g1 in activated and g2 in activated:
            out.append({"gates": [g1, g2], "name": name, "centers": [c1, c2]})
    return out


def _components(defined_centers: set[str], channels: list[dict[str, Any]]) -> list[set[str]]:
    """Connected components of defined centers, edges = defined channels."""
    adj: dict[str, set[str]] = {c: set() for c in defined_centers}
    for ch in channels:
        a, b = ch["centers"]
        adj[a].add(b)
        adj[b].add(a)
    seen: set[str] = set()
    comps: list[set[str]] = []
    for start in defined_centers:
        if start in seen:
            continue
        stack, comp = [start], set()
        while stack:
            n = stack.pop()
            if n in comp:
                continue
            comp.add(n)
            stack.extend(adj[n] - comp)
        seen |= comp
        comps.append(comp)
    return comps


def _determine_type(defined: set[str], comps: list[set[str]]) -> str:
    if not defined:
        return "Reflector"
    throat_comp = next((c for c in comps if "Throat" in c), set())
    throat_motor = bool(throat_comp & D.MOTORS)
    if "Sacral" in defined:
        return "Manifesting Generator" if throat_motor else "Generator"
    if throat_motor:
        return "Manifestor"
    return "Projector"


def _determine_authority(hd_type: str, defined: set[str]) -> str:
    if hd_type == "Reflector":
        return "Lunar"
    if "SolarPlexus" in defined:
        return "Emotional"
    if "Sacral" in defined:
        return "Sacral"
    if "Spleen" in defined:
        return "Splenic"
    if "Heart" in defined:
        return "Ego"
    if "G" in defined:
        return "Self-Projected"
    return "Mental"


def _cross_angle(pers_line: int, design_line: int) -> str:
    key = (pers_line, design_line)
    if key in D.RIGHT_ANGLE:
        return "Right Angle"
    if key in D.LEFT_ANGLE:
        return "Left Angle"
    if key in D.JUXTAPOSITION:
        return "Juxtaposition"
    return "Right Angle"  # fallback; the 12 valid profiles are all covered above


_DEFINITION_LABEL = {0: "No Definition", 1: "Single", 2: "Split",
                     3: "Triple Split", 4: "Quadruple Split"}


@dataclass
class Bodygraph:
    hd_type: str
    strategy: str
    authority: str
    authority_note: str
    profile: str           # e.g. "1/3"
    profile_lines: list[str]
    definition: str
    signature: str
    not_self: str
    defined_centers: list[str]
    undefined_centers: list[str]
    channels: list[dict[str, Any]]
    incarnation_cross: dict[str, Any]
    personality: dict[str, dict[str, Any]]  # body -> {gate, line, longitude}
    design: dict[str, dict[str, Any]]
    activated_gates: list[int]
    # carried for the transit overlay (not serialized)
    _coords: tuple[float, float] = field(default=(0.0, 0.0), repr=False)
    _tz: str = field(default="UTC", repr=False)
    _city: str = field(default="", repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.hd_type,
            "strategy": self.strategy,
            "authority": self.authority,
            "authority_note": self.authority_note,
            "profile": self.profile,
            "profile_lines": self.profile_lines,
            "definition": self.definition,
            "signature": self.signature,
            "not_self_theme": self.not_self,
            "defined_centers": [D.CENTER_DISPLAY[c] for c in self.defined_centers],
            "undefined_centers": [D.CENTER_DISPLAY[c] for c in self.undefined_centers],
            "open_centers_note": (
                "Undefined centers are where you take in and amplify the "
                "environment — the musical dimensions you feel most acutely."
            ),
            "channels": self.channels,
            "incarnation_cross": self.incarnation_cross,
            "personality": self.personality,
            "design": self.design,
        }

    def transit_overlay(self, when: datetime) -> dict[str, Any]:
        """Today's transiting gate activations + channels they complete in you."""
        lng, lat = self._coords
        subj = _offline_subject(when, lng, lat, self._tz, self._city)
        acts = _activations(subj)
        natal = set(self.activated_gates)

        transit_gates = []
        for body, info in acts.items():
            g = info["gate"]
            transit_gates.append({
                "body": body, "gate": g, "line": info["line"],
                "center": D.CENTER_DISPLAY[D.GATE_CENTER[g]],
                "keynote": D.GATE_KEYNOTE[g],
            })

        transit_gate_set = {info["gate"] for info in acts.values()}
        completed = []
        for (g1, g2), name, (c1, c2) in D.CHANNELS:
            t1, t2 = g1 in transit_gate_set, g2 in transit_gate_set
            n1, n2 = g1 in natal, g2 in natal
            full = (t1 or n1) and (t2 or n2)
            if not full:
                continue
            if not (t1 or t2):
                continue  # channel already wholly natal — not a transit event
            source = "transit×transit" if (t1 and t2) else "transit×natal"
            completed.append({
                "gates": [g1, g2], "name": name, "centers": [c1, c2],
                "via": source,
            })

        return {
            "transit_gates": transit_gates,
            "channels_activated": completed,
            "note": (
                "Transit gates colour the day's themes; channels_activated are "
                "channels today's transits complete in your chart — momentary "
                "definition you can lean into."
            ),
        }


def compute_bodygraph(natal: NatalData) -> Bodygraph:
    if not _KERYKEION_AVAILABLE:
        raise RuntimeError(
            "kerykeion is not installed. `pip install kerykeion` "
            "(needs pyswisseph build deps; see package README)."
        )

    tz = natal.timezone
    natal_subject = _online_natal_subject(natal)
    lng, lat = float(natal_subject.lng), float(natal_subject.lat)

    birth_dt = datetime(natal.year, natal.month, natal.day,
                        natal.hour, natal.minute, tzinfo=ZoneInfo(tz))
    natal_sun_long = natal_subject.sun.abs_pos
    design_dt = _design_datetime(birth_dt, natal_sun_long, lng, lat, tz, natal.city)
    design_subject = _offline_subject(design_dt, lng, lat, tz, natal.city)

    personality = _activations(natal_subject)
    design = _activations(design_subject)

    activated: set[int] = (
        {p["gate"] for p in personality.values()} |
        {p["gate"] for p in design.values()}
    )

    channels = _defined_channels(activated)
    defined_centers = {c for ch in channels for c in ch["centers"]}
    undefined_centers = [c for c in D.CENTERS if c not in defined_centers]
    comps = _components(defined_centers, channels)

    hd_type = _determine_type(defined_centers, comps)
    authority = _determine_authority(hd_type, defined_centers)

    pers_sun_line = personality["sun"]["line"]
    design_sun_line = design["sun"]["line"]
    profile = f"{pers_sun_line}/{design_sun_line}"
    profile_lines = [
        f"{pers_sun_line} — {D.PROFILE_LINE[pers_sun_line]}",
        f"{design_sun_line} — {D.PROFILE_LINE[design_sun_line]}",
    ]

    cross = {
        "angle": _cross_angle(pers_sun_line, design_sun_line),
        "gates": {
            "personality_sun": personality["sun"]["gate"],
            "personality_earth": personality["earth"]["gate"],
            "design_sun": design["sun"]["gate"],
            "design_earth": design["earth"]["gate"],
        },
    }

    # order centers for stable, head-to-root display
    order = {c: i for i, c in enumerate(D.CENTERS)}
    defined_sorted = sorted(defined_centers, key=lambda c: order[c])

    return Bodygraph(
        hd_type=hd_type,
        strategy=D.STRATEGY[hd_type],
        authority=authority,
        authority_note=D.AUTHORITY_NOTE[authority],
        profile=profile,
        profile_lines=profile_lines,
        definition=_DEFINITION_LABEL.get(len(comps), f"{len(comps)}-part"),
        signature=D.SIGNATURE[hd_type],
        not_self=D.NOT_SELF[hd_type],
        defined_centers=defined_sorted,
        undefined_centers=undefined_centers,
        channels=channels,
        incarnation_cross=cross,
        personality=personality,
        design=design,
        activated_gates=sorted(activated),
        _coords=(lng, lat),
        _tz=tz,
        _city=natal.city,
    )
