"""Sabian symbols — one evocative image per zodiac degree.

Each of the 360 degrees of the zodiac carries a Sabian symbol — a short poetic
image (channelled by Elsie Wheeler for Marc Edmund Jones, 1925; the wording here
is Dane Rudhyar's 1973 reformulation in *An Astrological Mandala*). It's a lovely
mood/imagery source: each planet's exact degree → an image → a feeling.

The verified 360-symbol table lives in `sabian_data.py` (generated from the
Rudhyar text, not fabricated). A planet at 15°20' Aries uses the "Aries 16"
symbol — i.e. the in-sign degree is rounded up.
"""
from __future__ import annotations

from typing import Any

from ..sabian_data import SYMBOLS

NAME = "sabian"

SIGN_ORDER = ["Ari", "Tau", "Gem", "Can", "Leo", "Vir",
              "Lib", "Sco", "Sag", "Cap", "Aqu", "Pis"]


def _symbol_number(sign: str, deg_in_sign: float) -> int | None:
    if sign not in SIGN_ORDER:
        return None
    n_in_sign = min(30, int(deg_in_sign) + 1)        # 1..30, round up
    return SIGN_ORDER.index(sign) * 30 + n_in_sign    # 1..360


def contribute(ctx: dict[str, Any]) -> dict[str, Any]:
    planets = ((ctx.get("transits") or {}).get("planets_now") or {})
    if not planets:
        return {"available": False, "status": "needs transits (kerykeion)"}

    rows = []
    for body, info in planets.items():
        num = _symbol_number(info.get("sign", ""), info.get("deg", 0.0))
        if num is None:
            continue
        rows.append({"body": body, "symbol_number": num,
                     "symbol": SYMBOLS.get(num)})

    if not SYMBOLS:  # data module empty/missing — fail soft
        return {"available": False,
                "status": "sabian_data.SYMBOLS is empty — regenerate the table"}

    by_body = {r["body"]: r["symbol"] for r in rows}
    # Sun & Moon images are the most useful imagery for the picker.
    luminaries = [by_body[b] for b in ("sun", "moon") if b in by_body]

    return {
        "available": True,
        "status": "live",
        "summary": (f"Sun: {by_body.get('sun', '?')}"
                    + (f" · Moon: {by_body['moon']}" if "moon" in by_body else "")),
        # Lead with the luminaries; the rest are available in data for deeper colour.
        "themes": luminaries,
        "data": {"bodies": rows},
    }
