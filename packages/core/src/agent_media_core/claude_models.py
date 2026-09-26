"""The models Claude Code offers, with its own line on what each is for.

The phone's model sheet should say what the terminal's `/model` picker says,
and that changes with every release — a list kept here would be wrong within
weeks. Claude Code hands it out: started headless with `--input-format
stream-json`, an `initialize` control request is answered with (among other
things) `models`, each `{value, resolvedModel, displayName, description}`.
The answer comes before any prompt, so asking costs no model call — only the
few seconds the process takes to start.

Cached on disk and thrown away when Claude Code's version changes, the same
way as the slash menu (slash_menu.py).
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

from ._paths import state_dir
from .slash_menu import claude_bin, claude_version

log = logging.getLogger(__name__)

#: A day at most; the version check is what really keeps it fresh.
CACHE_TTL_S = 24 * 3600

_INIT = {"type": "control_request", "request_id": "models",
         "request": {"subtype": "initialize"}}


def _cache_path() -> Path:
    return state_dir() / "claude-models.json"


def ask_claude(timeout: float = 60.0) -> list:
    """Claude Code's `/model` list — `[{value, resolvedModel, displayName,
    description, …}]` — or [] when it cannot be asked."""
    try:
        proc = subprocess.Popen(
            [claude_bin() or "claude", "-p", "--input-format", "stream-json",
             "--output-format", "stream-json", "--verbose"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)
    except OSError as e:  # noqa: BLE001 — no claude here; the caller copes
        log.warning("claude-models: cannot run claude (%s)", e)
        return []
    deadline = time.time() + timeout
    models: list = []
    try:
        proc.stdin.write(json.dumps(_INIT) + "\n")
        proc.stdin.flush()
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "control_response":
                answer = (event.get("response") or {}).get("response") or {}
                models = [m for m in (answer.get("models") or [])
                          if isinstance(m, dict) and m.get("value")]
                break
    except OSError as e:  # noqa: BLE001 — it went away before answering
        log.warning("claude-models: no answer (%s)", e)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return models


def models(*, refresh: bool = False) -> list:
    """`ask_claude`, cached until Claude Code's version changes."""
    path = _cache_path()
    version = claude_version()
    if not refresh:
        try:
            cached = json.loads(path.read_text())
        except (OSError, ValueError):
            cached = None
        if (cached and cached.get("models")
                and (not version or cached.get("version") == version)
                and time.time() - float(cached.get("at") or 0) < CACHE_TTL_S):
            return cached["models"]
    found = ask_claude()
    if not found:
        return []
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": time.time(), "version": version,
                                    "models": found}, indent=1))
    except OSError as e:  # noqa: BLE001 — uncached is still an answer
        log.debug("claude-models: cannot cache %s (%s)", path, e)
    return found
