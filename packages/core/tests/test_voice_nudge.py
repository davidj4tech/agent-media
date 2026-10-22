"""The hands-free nudge (voice_mode.py): the app's note is nobody's words,
and an answer of QUIET is never spoken."""

from agent_media_core import voice_mode
from agent_media_core.intake import _hook_stdin
from agent_media_core.intake import hook_claude_code as H
from agent_media_core.intake._text import strip_system_blocks
from agent_media_core.types import Source


def test_the_nudge_is_not_the_listeners_words():
    text = voice_mode.nudge_text()
    assert text.startswith("<voice-nudge>") and text.endswith("</voice-nudge>")
    assert strip_system_blocks(text) == ""
    # Stapled to real words, only the words are kept.
    assert strip_system_blocks(text + " yes please") == "yes please"


def test_the_note_covers_a_question_and_a_next_step():
    assert "ended with a question" in voice_mode.NUDGE
    assert "next step" in voice_mode.NUDGE
    assert voice_mode.QUIET in voice_mode.NUDGE


def test_is_quiet():
    assert voice_mode.is_quiet("[[quiet]]")
    assert voice_mode.is_quiet("  [[QUIET]]\n")
    assert not voice_mode.is_quiet("Want me to install it? [[quiet]]")
    assert not voice_mode.is_quiet("")


def _arm(tmp_path, monkeypatch):
    submitted = []
    monkeypatch.setattr(H, "submit_event", lambda e, **_: submitted.append(e) or "rid")
    monkeypatch.setenv("MEDIA_HOOK_NO_DETACH", "1")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(H, "_dedup_seen", lambda *a, **k: False)
    monkeypatch.setattr(H, "_session_name", lambda: "")
    return submitted


def test_a_quiet_answer_is_not_spoken(tmp_path, monkeypatch):
    submitted = _arm(tmp_path, monkeypatch)
    assert H._handle_stop({"last_assistant_message": "[[quiet]]",
                           "transcript_path": str(tmp_path / "t.jsonl"),
                           "session_id": "s1"}) == 0
    assert submitted == []


def test_a_quiet_answer_is_not_spoken_by_the_other_harnesses(monkeypatch):
    submitted = []
    monkeypatch.setattr(_hook_stdin, "submit_event", lambda e, **_: submitted.append(e))
    monkeypatch.setattr(_hook_stdin, "_load_env_file", lambda *a: None)
    monkeypatch.setenv("CODEX_TTS_ENABLED", "1")
    _hook_stdin.run(Source.CODEX, "CODEX", text="[[quiet]]")
    assert submitted == []
