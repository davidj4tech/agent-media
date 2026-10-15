"""Presets: saved ways to start a chat (server-contract §6.24).

A preset is the new-chat sheet filled in ahead of time — agent, login, model,
plan, persona and role (personas.py; "" for none) — plus the AGENTS.md a new project started from it gets and the words
the box starts with. The app applies one itself; the server reads a preset
only for its AGENTS.md (`agents_md_of`, from `/ask`'s `preset`).

Kept in `<state_dir>/presets.json`, a list in the order they were added, a
name replacing the preset of that name. "Profile" stays the harness login
(agent_media_core/harness_profiles.py); this is not that.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import auth

FIELDS = ("name", "agent", "profile", "model", "plan", "persona", "role", "agents_md", "prompt")


def store_path() -> Path:
    from agent_media_core import harness_profiles

    return harness_profiles.state_dir() / "presets.json"


def load() -> list[dict]:
    try:
        rows = json.loads(store_path().read_text())
    except (OSError, ValueError):
        return []
    return [_clean(r) for r in rows if isinstance(r, dict) and str(r.get("name") or "").strip()] \
        if isinstance(rows, list) else []


def _clean(r: dict) -> dict:
    return {"name": str(r.get("name") or "").strip()[:40],
            "agent": str(r.get("agent") or "").strip().lower(),
            "profile": str(r.get("profile") or "").strip(),
            "model": str(r.get("model") or "").strip(),
            "plan": r.get("plan") is True,
            "persona": str(r.get("persona") or "").strip(),
            "role": str(r.get("role") or "").strip(),
            "agents_md": str(r.get("agents_md") or ""),
            "prompt": str(r.get("prompt") or "")}


def _save(rows: list[dict]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=1) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def get(name: str) -> dict | None:
    name = (name or "").strip()
    return next((p for p in load() if p["name"] == name), None)


def agents_md_of(name: str) -> str:
    """A preset's AGENTS.md text; "" for none (the host's template then)."""
    p = get(name) if name else None
    return p["agents_md"] if p else ""


def listing(bearer: str) -> tuple[bool, dict]:
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    from . import new_project, personas

    return True, {"presets": load(), "agents_md_default": new_project.default_agents_md(),
                  **personas.listing()}


def put(body: dict, bearer: str) -> tuple[bool, dict]:
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    raw = str((body or {}).get("name") or "").strip()
    if not raw or len(raw) > 40:
        return False, {"error": "a preset's name is 1 to 40 characters", "status": 400}
    p = _clean(body)
    from . import panes

    if p["agent"] and p["agent"] not in panes.AGENT_COMMANDS:
        return False, {"error": f"unknown agent {p['agent']!r}", "status": 400}
    if p["persona"] or p["role"]:
        from . import personas

        why = personas.check(p["persona"], p["role"])
        if why:
            return False, {"error": why, "status": 400}
    rows = load()
    at = next((i for i, r in enumerate(rows) if r["name"] == p["name"]), None)
    if at is None:
        rows.append(p)
    else:
        rows[at] = p
    _save(rows)
    return True, {"preset": p}


def remove(name: str, bearer: str) -> tuple[bool, dict]:
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    rows = load()
    keep = [r for r in rows if r["name"] != (name or "").strip()]
    if len(keep) == len(rows):
        return False, {"error": f"no preset {name!r}", "status": 404}
    _save(keep)
    return True, {}
