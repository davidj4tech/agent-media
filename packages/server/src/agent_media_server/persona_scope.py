"""Where a persona or role is offered, and which models are trusted with a
private one (docs/proposals/2026-10-17-persona-scope.md, contract §6.24).

Three layers, the later winning field by field:

- **default** — the id's own front matter, from `persona-render --list
  --json` (personas.py): `render.compatible_adapters` as `agents`, and
  `projects`; and DEFAULT_TRUSTED below.
- **local** — this host's `~/.config/agent-media/personas.toml`
  (`MEDIA_PERSONAS_CONFIG`), edited by hand:

      trusted_models = ["venice/*"]
      [persona.zara]
      agents = ["pi"]
      projects = ["agent-media"]

- **user** — `<state_dir>/persona-scope.json`, the same shape, written by
  the app's Settings → Personas through `POST /personas/scope`.

A field no layer sets is no limit: `agents` None takes every agent,
`projects` None belongs to no project. `[]` is a real value (no agent).
"""

from __future__ import annotations

import fnmatch
import json
import os
import tomllib
from pathlib import Path

#: Models trusted with a private render unless a layer says otherwise: a
#: provider that keeps no logs, and models served on David's own hosts.
DEFAULT_TRUSTED = ["venice/*", "ollama/*", "local/*"]

#: The fields a layer may set for an id.
FIELDS = ("agents", "projects")


def local_path() -> Path:
    raw = (os.environ.get("MEDIA_PERSONAS_CONFIG") or "").strip()
    return Path(os.path.expanduser(raw or "~/.config/agent-media/personas.toml"))


def user_path() -> Path:
    from agent_media_core import harness_profiles

    return harness_profiles.state_dir() / "persona-scope.json"


def _strings(v) -> list[str] | None:
    if v is None:
        return None
    if not isinstance(v, list):
        return None
    return [str(x).strip() for x in v if str(x).strip()]


def _clean_layer(raw) -> dict:
    """`{"trusted_models": [...] | absent, "persona": {id: {field: [...]}}}`
    from whatever was read; anything malformed is left out."""
    out: dict = {"persona": {}}
    if not isinstance(raw, dict):
        return out
    if "trusted_models" in raw and _strings(raw.get("trusted_models")) is not None:
        out["trusted_models"] = _strings(raw["trusted_models"])
    ids = raw.get("persona")
    if isinstance(ids, dict):
        for pid, fields in ids.items():
            if not isinstance(fields, dict):
                continue
            row = {f: _strings(fields[f]) for f in FIELDS if f in fields and _strings(fields[f]) is not None}
            if row:
                out["persona"][str(pid)] = row
    return out


def local_layer() -> dict:
    try:
        with local_path().open("rb") as fh:
            return _clean_layer(tomllib.load(fh))
    except (OSError, tomllib.TOMLDecodeError):
        return _clean_layer(None)


def user_layer() -> dict:
    try:
        return _clean_layer(json.loads(user_path().read_text()))
    except (OSError, ValueError):
        return _clean_layer(None)


def default_layer(described: list[dict]) -> dict:
    """The front matter's layer, from `persona-render --list --json` rows."""
    out: dict = {"trusted_models": list(DEFAULT_TRUSTED), "persona": {}}
    for d in described:
        row = {}
        if d.get("adapters") is not None:
            row["agents"] = _strings(d["adapters"])
        if d.get("projects") is not None:
            row["projects"] = _strings(d["projects"])
        if row:
            out["persona"][str(d["id"])] = row
    return out


def merge(described: list[dict], layers: list[dict]) -> tuple[dict, list[str]]:
    """`(scope, trusted_models)`: per id `{"agents", "projects", "private"}`,
    each layer overriding the one before it field by field."""
    scope = {}
    for d in described:
        pid = str(d["id"])
        row = {"agents": None, "projects": None, "private": bool(d.get("private"))}
        for layer in layers:
            row.update(layer["persona"].get(pid, {}))
        scope[pid] = row
    trusted: list[str] = []
    for layer in layers:
        if "trusted_models" in layer:
            trusted = layer["trusted_models"]
    return scope, trusted


def resolve(described: list[dict]) -> dict:
    """Everything the app reads: the merged `scope` and `trusted_models`, and
    the three `layers` so Settings can show where each value comes from."""
    layers = {"default": default_layer(described), "local": local_layer(), "user": user_layer()}
    scope, trusted = merge(described, [layers["default"], layers["local"], layers["user"]])
    return {"scope": scope, "trusted_models": trusted, "layers": layers}


def takes(scope: dict, pid: str, agent: str) -> bool:
    """Whether `agent` takes `pid`; an id the scope does not know is taken
    (unknown ids are personas.check's to refuse)."""
    agents = (scope.get(pid) or {}).get("agents")
    return agents is None or agent in agents


def trusted(model: str, patterns: list[str]) -> bool:
    """Whether `model` (the /ask model id) matches a trusted pattern; no
    model — the agent's own default — is never trusted."""
    model = (model or "").strip()
    return bool(model) and any(fnmatch.fnmatchcase(model, p) for p in patterns)


def put(body: dict, bearer: str) -> tuple[bool, dict]:
    """`POST /personas/scope`: the user layer for one id (`{"id", "agents"?,
    "projects"?}`, a field `null` to clear it back to the layers below), or
    `{"trusted_models": [...] | null}`."""
    from . import auth, personas

    user, err = auth.gate(bearer)
    if not user:
        return False, err
    body = body if isinstance(body, dict) else {}
    layer = user_layer()
    if "trusted_models" in body:
        v = body["trusted_models"]
        if v is None:
            layer.pop("trusted_models", None)
        elif _strings(v) is None:
            return False, {"error": "trusted_models is a list of model patterns", "status": 400}
        else:
            layer["trusted_models"] = _strings(v)
    pid = str(body.get("id") or "").strip()
    if pid:
        lists = personas.listing()
        if pid not in lists["personas"] and pid not in lists["roles"]:
            return False, {"error": f"unknown persona or role {pid!r}", "status": 400}
        row = dict(layer["persona"].get(pid, {}))
        for f in FIELDS:
            if f not in body:
                continue
            if body[f] is None:
                row.pop(f, None)
            elif _strings(body[f]) is None:
                return False, {"error": f"{f} is a list", "status": 400}
            else:
                row[f] = _strings(body[f])
        if row:
            layer["persona"][pid] = row
        else:
            layer["persona"].pop(pid, None)
    path = user_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(layer, indent=2) + "\n")
    os.replace(tmp, path)
    return True, resolve(personas.described())
