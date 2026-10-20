"""Codex and Grok's own model catalogues, without hard-coded model names.

Both CLIs keep the account's offered models in models_cache.json. Read only
the public catalogue fields; credentials and custom-provider headers never
leave the host. Codex profiles have their own CODEX_HOME and catalogue.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")


def home(agent: str, profile: str = "") -> Path:
    from . import harness_profiles

    var = "CODEX_HOME" if agent == "codex" else "GROK_HOME"
    env = harness_profiles.env(agent, profile)
    return Path(env.get(var) or os.environ.get(var) or Path.home() / f".{agent}")


def read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def models(agent: str, profile: str = "") -> list[dict]:
    raw = read(home(agent, profile) / "models_cache.json").get("models", [])
    rows = []
    if agent == "grok" and isinstance(raw, dict):
        raw = [v.get("info", {}) for v in raw.values() if isinstance(v, dict)]
    if not isinstance(raw, list):
        return []
    for m in raw:
        if not isinstance(m, dict):
            continue
        mid = m.get("slug") if agent == "codex" else m.get("id")
        if not isinstance(mid, str) or not MODEL_ID.fullmatch(mid):
            continue
        if m.get("hidden") or (agent == "codex" and m.get("visibility", "list") != "list"):
            continue
        rows.append({"id": mid, "label": str(m.get("display_name") or m.get("name") or mid),
                     "note": str(m.get("description") or "")})
    return _latest_versions(rows)


def _latest_versions(rows: list[dict]) -> list[dict]:
    """Keep the newest numeric version of each exact model family.

    Suffixes remain part of the family: a newer Sol does not replace a
    Luna or Terra. Only visible, offered replacements count. Unversioned
    aliases and unfamiliar IDs pass through unchanged.
    """
    pattern = re.compile(r"([a-z][a-z-]*?)-(\d+(?:\.\d+)*)(-[a-z][a-z-]*)?")

    def version(mid: str):
        match = pattern.fullmatch(mid)
        if not match:
            return None
        parts = tuple(int(p) for p in match[2].split("."))
        # Treat 6 and 6.0 as the same release.
        while len(parts) > 1 and parts[-1] == 0:
            parts = parts[:-1]
        return (match[1], match[3] or ""), parts

    newest = {}
    for row in rows:
        parsed = version(row["id"])
        if parsed:
            family, number = parsed
            newest[family] = max(newest.get(family, ()), number)
    return [row for row in rows if (parsed := version(row["id"])) is None
            or parsed[1] == newest[parsed[0]]]


def allowed(model: str) -> str:
    return model if isinstance(model, str) and MODEL_ID.fullmatch(model) else ""


def current(session: str) -> tuple[str, float]:
    """Last effective model in a Codex turn context, or Grok's summary."""
    from . import harnesses

    found = harnesses.transcript(session)
    if not found:
        return "", 0.0
    agent, path = found
    try:
        if agent == "grok":
            summary = read(path.parent / "summary.json")
            info = summary.get("info") or {}
            model = info.get("model") or info.get("model_id") or ""
            return allowed(model), path.stat().st_mtime
        if agent != "codex":
            return "", 0.0
        with path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 256 * 1024))
            lines = f.read().splitlines()
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if row.get("type") == "turn_context":
                from datetime import datetime

                model = str((row.get("payload") or {}).get("model") or "")
                at = datetime.fromisoformat(str(row.get("timestamp")).replace("Z", "+00:00")).timestamp()
                return model, at
    except (OSError, ValueError, TypeError):
        pass
    return "", 0.0
