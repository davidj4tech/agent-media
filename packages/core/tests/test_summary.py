"""Unit tests for the optional LLM spoken-summary helper."""

import io
import json
import urllib.error

from agent_media_core.intake import _summary


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MEDIA_SPEECH_SUMMARY", raising=False)
    assert _summary.summary_enabled() is False


def test_enabled_when_set(monkeypatch):
    monkeypatch.setenv("MEDIA_SPEECH_SUMMARY", "1")
    assert _summary.summary_enabled() is True


def test_min_chars_default_and_override(monkeypatch):
    monkeypatch.delenv("MEDIA_SUMMARY_MIN_CHARS", raising=False)
    assert _summary.summary_min_chars() == _summary.DEFAULT_MIN_CHARS
    monkeypatch.setenv("MEDIA_SUMMARY_MIN_CHARS", "50")
    assert _summary.summary_min_chars() == 50
    monkeypatch.setenv("MEDIA_SUMMARY_MIN_CHARS", "not-an-int")
    assert _summary.summary_min_chars() == _summary.DEFAULT_MIN_CHARS


def test_empty_text_returns_none():
    assert _summary.summarize_for_speech("   ") is None


class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(payload, captured=None):
    def _open(req, timeout=None):
        if captured is not None:
            captured["url"] = req.full_url
            captured["auth"] = req.headers.get("Authorization")
            captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(payload)
    return _open


def test_happy_path_returns_content(monkeypatch):
    payload = {"choices": [{"message": {"content": "  A short spoken summary.  "}}]}
    monkeypatch.setattr(_summary.urllib.request, "urlopen", _fake_urlopen(payload))
    assert _summary.summarize_for_speech("a long reply") == "A short spoken summary."


def test_dedicated_base_url_and_key_used(monkeypatch):
    captured = {}
    payload = {"choices": [{"message": {"content": "ok"}}]}
    monkeypatch.setenv("MEDIA_SUMMARY_BASE_URL", "http://gw:4000/v1")
    monkeypatch.setenv("MEDIA_SUMMARY_API_KEY", "sk-local-xyz")
    monkeypatch.setenv("MEDIA_SUMMARY_MODEL", "local-abliterate")
    # A real OpenAI key that must NOT be used for the summary call.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai")
    monkeypatch.setattr(_summary.urllib.request, "urlopen",
                        _fake_urlopen(payload, captured))
    assert _summary.summarize_for_speech("a long reply") == "ok"
    assert captured["url"] == "http://gw:4000/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-local-xyz"
    assert captured["body"]["model"] == "local-abliterate"


def test_api_key_resolved_from_litellm_env(monkeypatch, tmp_path):
    # No explicit summary key + a real OpenAI key present (TTS key): the summary
    # must use the gateway master key from litellm.env, NOT the OpenAI key.
    keyfile = tmp_path / "litellm.env"
    keyfile.write_text("VENICE_API_KEY=v\nLITELLM_MASTER_KEY=sk-red5-master\n")
    monkeypatch.delenv("MEDIA_SUMMARY_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai-tts")
    monkeypatch.setenv("MEDIA_SUMMARY_KEY_FILE", str(keyfile))
    captured = {}
    payload = {"choices": [{"message": {"content": "ok"}}]}
    monkeypatch.setattr(_summary.urllib.request, "urlopen",
                        _fake_urlopen(payload, captured))
    assert _summary.summarize_for_speech("a long reply") == "ok"
    assert captured["auth"] == "Bearer sk-red5-master"


def test_http_error_returns_none(monkeypatch):
    def _raise(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "err", {}, io.BytesIO(b""))
    monkeypatch.setattr(_summary.urllib.request, "urlopen", _raise)
    assert _summary.summarize_for_speech("a long reply") is None


def test_timeout_returns_none(monkeypatch):
    def _raise(req, timeout=None):
        raise TimeoutError("timed out")
    monkeypatch.setattr(_summary.urllib.request, "urlopen", _raise)
    assert _summary.summarize_for_speech("a long reply") is None


def test_empty_content_returns_none(monkeypatch):
    payload = {"choices": [{"message": {"content": "   "}}]}
    monkeypatch.setattr(_summary.urllib.request, "urlopen", _fake_urlopen(payload))
    assert _summary.summarize_for_speech("a long reply") is None


def test_malformed_response_returns_none(monkeypatch):
    payload = {"unexpected": "shape"}
    monkeypatch.setattr(_summary.urllib.request, "urlopen", _fake_urlopen(payload))
    assert _summary.summarize_for_speech("a long reply") is None
