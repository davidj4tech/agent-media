"""The models a pi chat can run on, per harness profile.

pi lists what it can use with `pi --list-models`: a table, one row per model
whose provider has usable auth (a stored login, a key in the environment, or
a provider a `models.json` or an extension adds), `provider model context
max-out thinking images`. A harness profile is its own PI_CODING_AGENT_DIR,
so each lists its own: on red5 the default dir has the gateway and Meridian
(an extension), the `venice` profile its `venice` provider from models.json
(measured 15 Oct 2026, pi 1.2.0, with the canvas's environment — no API keys
in it, so the key-only providers are not listed).

An id here is `provider/model`, as pi's own `/model` takes it: a model's id
can repeat across providers, and OpenRouter's have a slash of their own. A
new chat is started with `--provider` and `--model`; a running one is told
`/model <provider/model>` (session_settings.py).

What a session runs is in its session file: a `model_change` entry when it
starts and on every switch, and each assistant message names its model.

The listing takes about a second; it is kept a few minutes per profile.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

CACHE_TTL_S = 600
#: A host with OpenRouter's key lists 400 models; a sheet is not a catalogue.
MAX_SHEET = 80

#: What `--provider`/`--model` and `/model` may be handed: provider/model,
#: nothing a shell or flag parser could take for anything else (the value is
#: on a tmux command line and typed into a pane).
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:/-]*")

_LOCK = threading.Lock()
#: profile -> (when, sheet, default)
_memo: dict[str, tuple[float, list, str]] = {}


def _dir(profile: str) -> Path:
    from . import harness_profiles, harnesses

    env = harness_profiles.env("pi", profile)
    return Path(env.get("PI_CODING_AGENT_DIR") or harnesses._pi_dir()).expanduser()


def _list(profile: str, timeout: float = 60.0) -> str:
    from . import harness_profiles, harnesses

    exe = harnesses.program("pi")
    if not exe:
        return ""
    try:
        done = subprocess.run([exe, "--list-models"], capture_output=True, text=True,
                              timeout=timeout,
                              env=harnesses.run_env(harness_profiles.env("pi", profile)))
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("pi-models: --list-models failed (%s)", e)
        return ""
    if done.returncode != 0:
        log.warning("pi-models: --list-models said %s", (done.stderr or "").strip()[-200:])
        return ""
    return done.stdout


def parse(listing: str) -> list[dict]:
    """`pi --list-models` as `[{id, provider, model, context}]`, in its order."""
    out: list[dict] = []
    for line in listing.splitlines():
        cols = line.split()
        if len(cols) < 2 or cols[0] == "provider":
            continue
        mid = f"{cols[0]}/{cols[1]}"
        if MODEL_ID.fullmatch(mid):
            out.append({"id": mid, "provider": cols[0], "model": cols[1],
                        "context": cols[2] if len(cols) > 2 else ""})
    return out


def sheet(listing: str, first: str = "") -> list[dict]:
    """The chip's sheet: the `first` provider's models (the profile's own
    default provider), then the rest as pi lists them; `MAX_SHEET` at most."""
    rows = parse(listing)
    rows.sort(key=lambda m: m["provider"] != first)
    return [{"id": m["id"], "label": m["model"], "provider": m["provider"],
             "note": m["provider"] + (f" · {m['context']} context" if m["context"] else "")}
            for m in rows[:MAX_SHEET]]


def configured(profile: str = "") -> tuple[str, str]:
    """`(provider, model)` the profile's settings start on; either may be ""."""
    try:
        data = json.loads((_dir(profile) / "settings.json").read_text())
    except (OSError, ValueError):
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    return str(data.get("defaultProvider") or ""), str(data.get("defaultModel") or "")


def models(profile: str = "") -> tuple[list[dict], str]:
    """`(sheet, default)` for `profile` ("" the default dir): its models, and
    the `provider/model` its "Default" runs ("" when its settings name none)."""
    with _LOCK:
        at, memo, default = _memo.get(profile, (0.0, [], ""))
        if memo and time.time() - at < CACHE_TTL_S:
            return memo, default
    provider, model = configured(profile)
    offered = sheet(_list(profile), provider)
    default = f"{provider}/{model}" if provider and model else ""
    if offered:
        with _LOCK:
            _memo[profile] = (time.time(), offered, default)
    return offered, default


def default_note(offered: list[dict], default: str) -> str:
    """The "Default" row's line: the model and its provider, or ""."""
    for m in offered:
        if m["id"] == default:
            return f"{m['label']} · {m['id'].split('/', 1)[0]}"
    return default


def allowed(model: str) -> str:
    """`model` if a chat may be put on it, else "": any well-formed id, as
    opencode's (a key added since the sheet was read lists more)."""
    model = (model or "").strip()
    return model if MODEL_ID.fullmatch(model) else ""


def start_flags(model: str) -> list[str]:
    """pi's arguments for a new chat on `model`; [] for a malformed one."""
    if not allowed(model):
        return []
    provider, name = model.split("/", 1)
    return ["--provider", provider, "--model", name]


def current(session: str) -> tuple[str, float]:
    """The `provider/model` a pi session is on, and when that was written:
    its last `model_change` (the one chosen; a virtual model's replies name
    the physical one), else its last reply's. ("", 0.0) when unknown."""
    from . import harnesses

    found = harnesses.transcript(session)
    if not found or found[0] != harnesses.PI:
        return "", 0.0
    try:
        with open(found[1], "rb") as f:
            lines = f.read().splitlines()
    except OSError:
        return "", 0.0
    reply = ("", 0.0)
    for raw in reversed(lines):
        if b'"model_change"' not in raw and b'"assistant"' not in raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if obj.get("type") == "model_change" and obj.get("provider") and obj.get("modelId"):
            return f"{obj['provider']}/{obj['modelId']}", _when(obj.get("timestamp"))
        msg = obj.get("message") or {}
        if (not reply[0] and obj.get("type") == "message" and msg.get("role") == "assistant"
                and msg.get("provider") and msg.get("model")):
            reply = (f"{msg['provider']}/{msg['model']}", _when(obj.get("timestamp")))
    return reply


def _when(stamp) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def forget() -> None:
    """Drop the cached sheets."""
    with _LOCK:
        _memo.clear()
