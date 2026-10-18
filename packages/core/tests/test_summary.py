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


# --- describe: a block the model took for a request (18 Oct 2026) ---------------

import pathlib as _pathlib

_REPLY = json.loads((_pathlib.Path(__file__).parent / "fixtures" /
                     "described_code_reply.json").read_text())["reply"]
#: What claude-haiku-4-5 said, verbatim, for both blocks of that reply when
#: each was sent bare — and what the voice then read out as their descriptions.
_REFUSALS = [
    "I don't see a code block in your message to describe. Could you please "
    "share the text-to-speech code you'd like me to describe?",
    "I don't see a code block for text-to-speech in your message. Could you "
    "please share the code you'd like me to describe?",
    "I don't see a code block to describe in your message.",
]


def test_describe_sends_the_block_tagged_as_the_thing_to_describe(monkeypatch):
    """A directory tree and an ini file went to the model bare, and it answered
    as if asked for code it had not been given. The block now goes inside
    <code> tags, and the prompt says that is what it is."""
    from agent_media_core.intake._text import strip_markdown
    sent = []

    def chat(prompt, user_text, timeout, model=None):
        sent.append((prompt, user_text))
        return "A directory tree with three project folders."
    monkeypatch.setattr(_summary, "_chat", chat)
    monkeypatch.delenv("MEDIA_DESCRIBE_LOG", raising=False)
    monkeypatch.delenv("MEDIA_DESCRIBE_CODE_PROMPT", raising=False)
    strip_markdown(_REPLY, describe=True)
    assert len(sent) == 2
    tree, ini = (u for _p, u in sent)
    assert tree.startswith("<code>\n~/projects/\n") and tree.endswith("\n</code>")
    assert ini.startswith('<code>\n[includeIf "gitdir:~/projects/spl/"]')
    assert all("<code> tags" in p for p, _u in sent)


def test_a_reply_that_is_not_a_description_falls_back_to_the_placeholder(monkeypatch):
    """"I don't see a code block…" was spoken twice in one reply as if it
    described the block. Anything that is a question back or says there is no
    code is not a description: the placeholder is said instead."""
    from agent_media_core.intake._text import strip_markdown
    replies = iter(_REFUSALS[:2])
    monkeypatch.setattr(_summary, "_chat", lambda *a, **k: next(replies))
    monkeypatch.delenv("MEDIA_DESCRIBE_LOG", raising=False)
    out = strip_markdown(_REPLY, describe=True)
    assert "I don't see" not in out and "share" not in out
    assert "code block, 4 lines, omitted." in out
    assert "ini code block, 4 lines, omitted." in out
    for r in _REFUSALS:
        assert not _summary._is_description(r), r


def test_real_descriptions_still_pass():
    for ok in ("A directory tree showing three project folders.",
               "A Git configuration file that conditionally includes per-folder settings.",
               "a shell command that force-pushes the main branch",
               "A function that returns no value when the input is empty.",
               "a three-row table comparing hosts by sync status"):
        assert _summary._is_description(ok), ok
    assert not _summary._is_description("")
    assert not _summary._is_description("What would you like described?")
