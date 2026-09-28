"""API keys for opencode's providers, set from the app's Coding agents page.

opencode's own free models need nothing; a key for a provider adds that
provider's free models to the sheet (opencode_models.py) — OpenRouter's
`…:free`, 50 requests a day on a free account, 1000 once it has had credit.
opencode keeps keys in `auth.json` beside its database, one entry per
provider, `{"<provider>": {"type": "api", "key": "…"}}` — what `opencode auth
login` writes, and what `opencode auth list` counts (measured 28 Sep 2026 on
1.18.32: a key written there made OpenRouter's 17 free models appear).

A key is checked with the provider before it is kept, so a typo is said at
once rather than as a failed turn later. Written atomically, owner-only.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

#: The providers the page offers: their name, where a key is made, and the
#: URL that says whether a key is good (GET with it as the bearer).
PROVIDERS = {
    "openrouter": {
        "name": "OpenRouter",
        "signup": "https://openrouter.ai/keys",
        "check": "https://openrouter.ai/api/v1/key",
    },
}

_KEY = re.compile(r"[\x21-\x7e]{16,400}")


def auth_path() -> Path:
    from . import harnesses

    return harnesses.opencode_db().parent / "auth.json"


def stored() -> dict:
    try:
        data = json.loads(auth_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def providers() -> list[dict]:
    """`[{id, name, signup, set}]`, for the page. Never the key itself."""
    have = stored()
    return [{"id": pid, "name": p["name"], "signup": p["signup"],
             "set": isinstance(have.get(pid), dict) and bool(have[pid].get("key"))}
            for pid, p in PROVIDERS.items()]


def well_formed(key: str) -> bool:
    return bool(_KEY.fullmatch(key or ""))


def check(provider: str, key: str, timeout: float = 10.0) -> tuple[bool | None, str]:
    """`(True, note)` a good key, `(False, why)` a refused one, `(None, why)`
    when the provider could not be asked."""
    p = PROVIDERS[provider]
    req = urllib.request.Request(p["check"], headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, f"{p['name']} refused this key"
        return None, f"{p['name']} answered {e.code}"
    except (OSError, ValueError) as e:
        return None, f"could not reach {p['name']} ({e.__class__.__name__})"
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict) and data.get("is_free_tier"):
        return True, "Free account: its free models, 50 requests a day"
    return True, "Account with credit: its free models, 1000 requests a day"


def _write(data: dict) -> None:
    path = auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".auth.", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(provider: str, key: str) -> None:
    """Keep `key` for `provider`, leaving every other provider's as it was."""
    data = stored()
    data[provider] = {"type": "api", "key": key}
    _write(data)


def remove(provider: str) -> bool:
    data = stored()
    if provider not in data:
        return False
    del data[provider]
    _write(data)
    return True
