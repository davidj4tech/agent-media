"""opencode's provider keys (opencode_keys.py): kept in its auth.json beside
its database, checked with the provider first, never handed back."""

from __future__ import annotations

import io
import json
import stat
import urllib.error

import pytest

from agent_media_core import opencode_keys as ok_


KEY = "sk-or-v1-" + "a" * 64


@pytest.fixture()
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path / "opencode" / "auth.json"


def answer(monkeypatch, code=200, body=None):
    seen = {}

    def urlopen(req, timeout=0):
        seen["auth"] = req.get_header("Authorization")
        seen["url"] = req.full_url
        if code != 200:
            raise urllib.error.HTTPError(req.full_url, code, "no", {}, io.BytesIO(b"{}"))
        return io.BytesIO(json.dumps(body or {}).encode())
    monkeypatch.setattr(ok_.urllib.request, "urlopen", urlopen)
    return seen


def test_saved_where_opencode_reads_it_and_owner_only(data):
    data.parent.mkdir(parents=True)
    data.write_text(json.dumps({"anthropic": {"type": "api", "key": "keep-me"}}))
    ok_.save("openrouter", KEY)
    saved = json.loads(data.read_text())
    assert saved == {"anthropic": {"type": "api", "key": "keep-me"},
                     "openrouter": {"type": "api", "key": KEY}}
    assert stat.S_IMODE(data.stat().st_mode) == 0o600
    assert ok_.providers() == [{"id": "openrouter", "name": "OpenRouter",
                                "signup": "https://openrouter.ai/keys", "set": True}]
    assert ok_.remove("openrouter") and not ok_.remove("openrouter")
    assert json.loads(data.read_text()) == {"anthropic": {"type": "api", "key": "keep-me"}}


def test_checked_with_the_provider(data, monkeypatch):
    seen = answer(monkeypatch, body={"data": {"is_free_tier": True}})
    assert ok_.check("openrouter", KEY) == (True, "Free account: its free models, 50 requests a day")
    assert seen["auth"] == f"Bearer {KEY}" and seen["url"] == "https://openrouter.ai/api/v1/key"
    answer(monkeypatch, body={"data": {"is_free_tier": False}})
    assert "1000 requests a day" in ok_.check("openrouter", KEY)[1]
    answer(monkeypatch, code=401)
    assert ok_.check("openrouter", KEY) == (False, "OpenRouter refused this key")
    answer(monkeypatch, code=503)
    assert ok_.check("openrouter", KEY)[0] is None


def test_only_something_key_shaped():
    assert ok_.well_formed(KEY)
    for bad in ("", "short", "has a space in it 1234567890", "x" * 401, "tab\tinside-0123456789"):
        assert not ok_.well_formed(bad)
