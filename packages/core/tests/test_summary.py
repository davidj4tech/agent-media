"""Unit tests for the optional LLM spoken-summary helper."""

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


def test_bogus_interpreter_returns_none(monkeypatch):
    monkeypatch.setenv("MEDIA_SUMMARY_PYTHON", "/nonexistent/python-xyz-123")
    assert _summary.summarize_for_speech("some sufficiently long text") is None


def test_happy_path_returns_stdout(monkeypatch):
    class FakeProc:
        returncode = 0
        stdout = b"  A short spoken summary.  "

    monkeypatch.setenv("MEDIA_SUMMARY_PYTHON", "python3")
    monkeypatch.setattr(_summary.subprocess, "run", lambda *a, **k: FakeProc())
    assert _summary.summarize_for_speech("a long reply") == "A short spoken summary."


def test_nonzero_exit_returns_none(monkeypatch):
    class FakeProc:
        returncode = 1
        stdout = b"whatever"

    monkeypatch.setenv("MEDIA_SUMMARY_PYTHON", "python3")
    monkeypatch.setattr(_summary.subprocess, "run", lambda *a, **k: FakeProc())
    assert _summary.summarize_for_speech("a long reply") is None


def test_empty_stdout_returns_none(monkeypatch):
    class FakeProc:
        returncode = 0
        stdout = b"   "

    monkeypatch.setenv("MEDIA_SUMMARY_PYTHON", "python3")
    monkeypatch.setattr(_summary.subprocess, "run", lambda *a, **k: FakeProc())
    assert _summary.summarize_for_speech("a long reply") is None


def test_timeout_returns_none(monkeypatch):
    import subprocess as _sp

    def _raise(*a, **k):
        raise _sp.TimeoutExpired(cmd="x", timeout=1)

    monkeypatch.setenv("MEDIA_SUMMARY_PYTHON", "python3")
    monkeypatch.setattr(_summary.subprocess, "run", _raise)
    assert _summary.summarize_for_speech("a long reply") is None
