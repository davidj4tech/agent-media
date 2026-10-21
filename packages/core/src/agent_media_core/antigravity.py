"""Managed Antigravity transcripts and model sheet, independent of Gemini CLI's store.

The native conversation ID belongs to agy; the app ID stays stable across
process restarts. The session host exports messages in our common file schema.
"""
from __future__ import annotations
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime, timezone

log = logging.getLogger(__name__)

CACHE_TTL_S = 600
MODEL_ID = re.compile(r"[a-z0-9][a-z0-9._:/-]{0,199}")

_LOCK = threading.Lock()
_memo: tuple[float, list, str] = (0.0, [], "")


def root() -> Path:
    return Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'agent-media/antigravity'


def path(session: str) -> Path:
    if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', session):
        raise ValueError('invalid Antigravity session ID')
    return root() / f'{session}.jsonl'


def append(session: str, record: dict) -> None:
    import uuid
    p = path(session)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = {'uuid': str(uuid.uuid4()), 'timestamp': datetime.now(timezone.utc).isoformat(), **record}
    fd = os.open(p, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'a') as f:
        f.write(json.dumps(record, separators=(',', ':')) + '\n')


def _list(timeout: float = 30.0) -> str:
    from . import harnesses

    exe = os.environ.get("MEDIA_SESSIOND_ANTIGRAVITY") or harnesses.program("antigravity")
    if not exe:
        return ""
    try:
        done = subprocess.run([exe, "models"], capture_output=True, text=True,
                              timeout=timeout, env=harnesses.run_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("antigravity-models: models failed (%s)", e)
        return ""
    if done.returncode != 0:
        log.warning("antigravity-models: models said %s", (done.stderr or "").strip()[-200:])
        return ""
    return done.stdout


def parse(listing: str) -> list[dict]:
    """`agy models` as `[{id, label, note}]`, in its order."""
    text = (listing or "").strip()
    if text.startswith("{"):
        try:
            raw = ((json.loads(text).get("command") or {}).get("data") or {}).get("models") or []
            return [{"id": m["id"], "label": str(m.get("label") or m["id"]), "note": ""}
                    for m in raw if isinstance(m, dict) and isinstance(m.get("id"), str)
                    and MODEL_ID.fullmatch(m["id"])]
        except ValueError:
            return []
    out: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split("\t", 1) if "\t" in line else re.split(r"\s{2,}|\s+", line, maxsplit=1)
        mid = parts[0].strip()
        if not MODEL_ID.fullmatch(mid):
            continue
        label = parts[1].strip() if len(parts) > 1 and parts[1].strip() else mid
        out.append({"id": mid, "label": label, "note": ""})
    return out


def configured() -> str:
    """The model Antigravity's settings.json starts on, or ""."""
    home = Path(os.environ.get("GEMINI_CLI_HOME") or Path.home())
    try:
        data = json.loads((home / ".gemini/antigravity-cli/settings.json").read_text())
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    mid = str(data.get("defaultModelId") or data.get("model") or "").strip()
    return mid if MODEL_ID.fullmatch(mid) else ""


def models() -> tuple[list[dict], str]:
    """`(sheet, default)`: the models `agy models` offers, and what Default runs."""
    global _memo
    with _LOCK:
        at, memo, default = _memo
        if memo and time.time() - at < CACHE_TTL_S:
            return memo, default
    offered = parse(_list())
    default = configured() or (offered[0]["id"] if offered else "")
    if offered:
        with _LOCK:
            _memo = (time.time(), offered, default)
    return offered, default


def default_note(offered: list[dict], default: str) -> str:
    """The "Default" row's line: the model's human-readable label, or ""."""
    for m in offered:
        if m["id"] == default:
            return m["label"]
    return default


def allowed(model: str) -> str:
    model = (model or "").strip()
    return model if MODEL_ID.fullmatch(model) else ""


def forget() -> None:
    """Drop the cached sheet."""
    global _memo
    with _LOCK:
        _memo = (0.0, [], "")
