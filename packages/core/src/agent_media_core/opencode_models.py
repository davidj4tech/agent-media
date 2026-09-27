"""The models a new opencode chat can be started on, free ones marked.

opencode is the way into Sasonica without a paid plan: its own provider
(`opencode/…`, "Zen") has models that answer with no key and no sign-in —
`big-pickle` is a bare install's default — and OpenRouter names its free
ones `…:free`. `opencode models --verbose` lists every model the host can
reach, each a `provider/id` line and its JSON (name, cost, capabilities).

The sheet is the free ones that can use tools, since an agent that cannot
edit a file is not much of one. Everything else the host has keys for can be
hundreds of models (OpenRouter alone is 300+), so it is not listed; the
"Default" row is whatever opencode's own config picks, named in its note.

A price of 0 is only taken as free from those two: a custom provider (a
local gateway) has no prices at all, which reads as 0 and means "unknown".

Asked at most every few minutes: the listing takes ~3 s.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time

log = logging.getLogger(__name__)

CACHE_TTL_S = 600
FREE_DEFAULT = "opencode/big-pickle"

#: What `-m` may be handed: provider/model, nothing a shell or flag parser
#: could take for anything else (the value is on a tmux command line).
MODEL_ID = re.compile(r"[a-z0-9][a-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:/-]*")

_LOCK = threading.Lock()
_memo: tuple[float, list, str] = (0.0, [], "")


def _run(argv: list[str], timeout: float) -> str:
    from . import harnesses

    exe = harnesses.program("opencode")
    if not exe:
        return ""
    try:
        done = subprocess.run([exe, *argv], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("opencode-models: %s failed (%s)", argv[0], e)
        return ""
    return done.stdout if done.returncode == 0 else ""


def parse(listing: str) -> list[dict]:
    """`opencode models --verbose` as `[{id, name, cost, toolcall, status}]`.

    Each model is its `provider/id` on a line of its own, then a JSON object.
    """
    out: list[dict] = []
    dec = json.JSONDecoder()
    i, n = 0, len(listing)
    while i < n:
        nl = listing.find("\n", i)
        line = listing[i:nl if nl >= 0 else n].strip()
        i = n if nl < 0 else nl + 1
        if not MODEL_ID.fullmatch(line):
            continue
        j = listing.find("{", i)
        if j < 0:
            break
        try:
            obj, end = dec.raw_decode(listing, j)
        except ValueError:
            continue
        i = end
        cost = obj.get("cost") or {}
        out.append({
            "id": line,
            "name": str(obj.get("name") or line.split("/", 1)[1]),
            "cost": (float(cost.get("input") or 0), float(cost.get("output") or 0)),
            "toolcall": bool((obj.get("capabilities") or {}).get("toolcall")),
            "status": str(obj.get("status") or ""),
        })
    return out


def is_free(m: dict) -> bool:
    provider = m["id"].split("/", 1)[0]
    if provider == "openrouter":
        return m["id"].endswith(":free")
    return provider == "opencode" and m["cost"] == (0.0, 0.0)


def sheet(listing: str) -> list[dict]:
    """The chip's sheet from a listing: free, tool-using, not deprecated;
    opencode's own first (no key needed), then by name."""
    free = [m for m in parse(listing)
            if is_free(m) and m["toolcall"] and m["status"] != "deprecated"]
    free.sort(key=lambda m: (not m["id"].startswith("opencode/"), m["name"].lower()))
    return [{"id": m["id"], "label": m["name"], "note": "Free"} for m in free]


def configured() -> str:
    """The model opencode's own config starts on, or "" (then big-pickle)."""
    try:
        return str(json.loads(_run(["debug", "config"], 30.0) or "{}").get("model") or "")
    except ValueError:
        return ""


def models() -> tuple[list[dict], str]:
    """`(sheet, default)`: the free models, and what "Default" runs."""
    global _memo
    with _LOCK:
        at, memo, default = _memo
        if memo and time.time() - at < CACHE_TTL_S:
            return memo, default
    offered = sheet(_run(["models", "--verbose"], 60.0))
    default = configured() or FREE_DEFAULT
    if offered:
        with _LOCK:
            _memo = (time.time(), offered, default)
    return offered, default


def default_note(offered: list[dict], default: str) -> str:
    """The "Default" row's line: the model's name, and Free when it is."""
    for m in offered:
        if m["id"] == default:
            return f"{m['label']} · Free"
    return default


def allowed(model: str) -> str:
    """`model` if a new chat may be started on it, else "".

    One from the sheet; or any well-formed id, since a host with keys may
    name a paid one (the sheet does not list them, the chat can still ask)."""
    model = (model or "").strip()
    return model if MODEL_ID.fullmatch(model) else ""
