from agent_media_core import harnesses


def test_resumed_codex_is_found_without_open_rollout(monkeypatch):
    sid = "01a15b8b-f5c5-7e70-9f23-fa7773812afe"
    monkeypatch.setattr(harnesses, "_argv", lambda pid: ["codex", "resume", sid])
    monkeypatch.setattr(harnesses.glob, "glob", lambda pattern: [])
    assert harnesses._codex_session("123") == sid


def test_resume_last_does_not_guess_a_session(monkeypatch):
    monkeypatch.setattr(harnesses, "_argv", lambda pid: ["codex", "resume", "--last"])
    monkeypatch.setattr(harnesses.glob, "glob", lambda pattern: [])
    assert harnesses._codex_session("123") == ""
