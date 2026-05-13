from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass


@dataclass
class NatalData:
    year: int
    month: int
    day: int
    hour: int
    minute: int
    city: str
    nation: str  # ISO country code, e.g. "AU"
    timezone: str  # IANA, e.g. "Australia/Melbourne"


def _from_sacred_brain() -> NatalData | None:
    if not shutil.which("sacred-search"):
        return None
    try:
        out = subprocess.run(
            ["sacred-search", "natal chart birth date time place", "david", "5"],
            capture_output=True, text=True, timeout=8,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    # Sacred Brain returns free-text memories; downstream code (Claude in the
    # skill wrapper) parses them. Here we only succeed if we find a structured
    # natal JSON block in any memory body.
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            return NatalData(**d)
        except TypeError:
            continue
    return None


def _prompt() -> NatalData:
    sys.stderr.write(
        "No natal data found in Sacred Brain.\n"
        "Provide birth details (one per line):\n"
    )
    def ask(label: str, cast=str):
        sys.stderr.write(f"  {label}: ")
        sys.stderr.flush()
        return cast(input().strip())
    return NatalData(
        year=ask("year", int),
        month=ask("month", int),
        day=ask("day", int),
        hour=ask("hour (24h)", int),
        minute=ask("minute", int),
        city=ask("city"),
        nation=ask("nation (ISO code, e.g. AU)"),
        timezone=ask("timezone (IANA, e.g. Australia/Melbourne)"),
    )


def load_natal() -> NatalData:
    n = _from_sacred_brain()
    if n is not None:
        return n
    return _prompt()


def to_dict(n: NatalData) -> dict:
    return asdict(n)
