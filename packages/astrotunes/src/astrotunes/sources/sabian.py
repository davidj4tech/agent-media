"""Sabian symbols — one evocative image per zodiac degree.

Each of the 360 degrees of the zodiac carries a Sabian symbol (Marc Edmund
Jones / Elsie Wheeler, 1925) — a short poetic image. They're a lovely mood/
imagery source: each planet's exact degree → an image → a feeling word.

This is a *scaffold*. The degree→symbol index math is implemented and the slot
is wired, but the verified 360-symbol text table is not loaded yet (I won't
fabricate 360 lines of esoterica — same discipline as the HD gate tables). Drop
a verified `SYMBOLS[1..360]` table in and this goes live; until then it reports
the symbol *number* per body so you can see the mechanism working.
"""
from __future__ import annotations

from typing import Any

NAME = "sabian"

SIGN_ORDER = ["Ari", "Tau", "Gem", "Can", "Leo", "Vir",
              "Lib", "Sco", "Sag", "Cap", "Aqu", "Pis"]

# SYMBOLS[absolute_degree 1..360] = "image text". Empty until a verified table
# is loaded. (Convention: a planet at 15°20' Aries uses the "Aries 16" symbol —
# i.e. round the in-sign degree up.)
SYMBOLS: dict[int, str] = {}


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

    have_text = bool(SYMBOLS)
    return {
        # Until a verified symbol table is loaded there's no text to use, so the
        # source stays "not available" for the picker but exposes the mechanism.
        "available": have_text,
        "status": "live" if have_text else
                  "scaffold — load verified 360 Sabian symbol table to enable",
        "summary": f"{len(rows)} bodies mapped to Sabian degrees"
                   + ("" if have_text else " (numbers only)"),
        "themes": [r["symbol"] for r in rows if r["symbol"]],
        "data": {"bodies": rows},
    }
