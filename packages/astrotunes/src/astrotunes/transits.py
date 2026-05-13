from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

try:
    from kerykeion import AstrologicalSubject
    _KERYKEION_AVAILABLE = True
except ImportError:
    AstrologicalSubject = None  # type: ignore
    _KERYKEION_AVAILABLE = False

from .natal.sacred import NatalData


@dataclass
class Transits:
    moon_phase_deg: float  # 0 = new moon, 180 = full
    moon_phase_label: str
    planets_now: dict[str, dict[str, Any]]   # planet -> {sign, deg, retrograde}
    aspects_to_natal: list[dict[str, Any]]   # [{transit, natal, aspect, orb}]
    sun_sign_now: str

    def to_dict(self) -> dict:
        return {
            "moon_phase_deg": self.moon_phase_deg,
            "moon_phase_label": self.moon_phase_label,
            "planets_now": self.planets_now,
            "aspects_to_natal": self.aspects_to_natal,
            "sun_sign_now": self.sun_sign_now,
        }


def _phase_label(deg: float) -> str:
    deg = deg % 360
    if deg < 22.5 or deg >= 337.5: return "new"
    if deg < 67.5: return "waxing crescent"
    if deg < 112.5: return "first quarter"
    if deg < 157.5: return "waxing gibbous"
    if deg < 202.5: return "full"
    if deg < 247.5: return "waning gibbous"
    if deg < 292.5: return "last quarter"
    return "waning crescent"


def compute(natal: NatalData, when: datetime | None = None) -> Transits:
    if not _KERYKEION_AVAILABLE:
        raise RuntimeError(
            "kerykeion is not installed. `pip install kerykeion` "
            "(needs pyswisseph build deps; see package README)."
        )
    when = when or datetime.now(ZoneInfo(natal.timezone))

    natal_subject = AstrologicalSubject(
        "Natal", year=natal.year, month=natal.month, day=natal.day,
        hour=natal.hour, minute=natal.minute,
        city=natal.city, nation=natal.nation, tz_str=natal.timezone,
    )
    transit_subject = AstrologicalSubject(
        "Transit", year=when.year, month=when.month, day=when.day,
        hour=when.hour, minute=when.minute,
        city=natal.city, nation=natal.nation, tz_str=natal.timezone,
    )

    planets = ["sun", "moon", "mercury", "venus", "mars",
               "jupiter", "saturn", "uranus", "neptune", "pluto"]
    planets_now: dict[str, dict[str, Any]] = {}
    for p in planets:
        body = getattr(transit_subject, p)
        planets_now[p] = {
            "sign": body.sign,
            "deg": round(body.position, 2),
            "retrograde": bool(getattr(body, "retrograde", False)),
        }

    moon_long = transit_subject.moon.abs_pos
    sun_long = transit_subject.sun.abs_pos
    phase_deg = (moon_long - sun_long) % 360

    aspects = _aspects(transit_subject, natal_subject, planets)

    return Transits(
        moon_phase_deg=round(phase_deg, 2),
        moon_phase_label=_phase_label(phase_deg),
        planets_now=planets_now,
        aspects_to_natal=aspects,
        sun_sign_now=transit_subject.sun.sign,
    )


_ASPECTS = {0: ("conjunction", 8), 60: ("sextile", 4), 90: ("square", 6),
            120: ("trine", 6), 180: ("opposition", 8)}


def _aspects(transit, natal, planets) -> list[dict[str, Any]]:
    out = []
    for tp in planets:
        t_body = getattr(transit, tp)
        for np_ in planets:
            n_body = getattr(natal, np_)
            diff = abs(t_body.abs_pos - n_body.abs_pos) % 360
            if diff > 180:
                diff = 360 - diff
            for angle, (name, orb) in _ASPECTS.items():
                if abs(diff - angle) <= orb:
                    out.append({
                        "transit": tp, "natal": np_, "aspect": name,
                        "orb": round(abs(diff - angle), 2),
                    })
                    break
    return out
