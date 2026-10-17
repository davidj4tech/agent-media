"""The context warnings: the window the status line was told, else a guess."""
import io
import json

from agent_media_core.intake import context


def _run(fn, monkeypatch, capsys, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    fn()
    return capsys.readouterr().out


def _transcript(tmp_path, model, used):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "assistant", "message": {
        "model": model, "usage": {"input_tokens": used}}}) + "\n")
    return str(t)


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("CLAUDE_CONTEXT_WINDOW", raising=False)


def test_a_known_1m_model_is_not_warned_at_120k(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    t = _transcript(tmp_path, "claude-opus-5-5", 120_000)
    assert _run(context.main, monkeypatch, capsys,
                {"transcript_path": t, "session_id": "a"}) == ""


def test_an_unknown_model_is_taken_as_200k(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    t = _transcript(tmp_path, "some-model", 120_000)
    out = _run(context.main, monkeypatch, capsys, {"transcript_path": t, "session_id": "b"})
    assert "60% full (120k of 200k)" in out


def test_past_200k_means_the_1m_window(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    t = _transcript(tmp_path, "some-model", 600_000)
    out = _run(context.main, monkeypatch, capsys, {"transcript_path": t, "session_id": "c"})
    assert "60% full (600k of 1000k)" in out


def test_the_window_the_status_line_was_told_wins(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    line = _run(context.main_statusline, monkeypatch, capsys, {
        "session_id": "d", "model": {"display_name": "Opus 5.5"},
        "context_window": {"context_window_size": 200_000, "used_percentage": 61.2}})
    assert line.strip() == "Opus 5.5  ctx 61% of 200k"
    t = _transcript(tmp_path, "claude-opus-5-5", 120_000)
    out = _run(context.main, monkeypatch, capsys, {"transcript_path": t, "session_id": "d"})
    assert "60% full (120k of 200k)" in out


def test_each_band_is_told_once(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    t = _transcript(tmp_path, "some-model", 150_000)
    hook = {"transcript_path": t, "session_id": "e"}
    assert "75% full" in _run(context.main, monkeypatch, capsys, hook)
    assert _run(context.main, monkeypatch, capsys, hook) == ""


def test_nonsense_in_prints_nothing(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("nope"))
    context.main()
    context.main_statusline()
    assert capsys.readouterr().out == ""


def test_a_harness_that_knows_both_numbers_gives_them(tmp_path, monkeypatch, capsys):
    """pi's extension: its own count and window, no transcript."""
    _env(tmp_path, monkeypatch)
    out = _run(context.main, monkeypatch, capsys, {
        "session_id": "pi-x", "context_tokens": 230_000, "context_window": 272_000})
    assert "85% full (230k of 272k)" in out


def test_codex_gives_its_own_window(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    t = tmp_path / "rollout.jsonl"
    t.write_text("\n".join(json.dumps(r) for r in (
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 190_000, "total_tokens": 191_000},
            "model_context_window": 258_400}}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
    )) + "\n")
    note = context.note_for({"transcript_path": str(t), "session_id": "cx",
                             "hook_event_name": "UserPromptSubmit"})
    out = json.loads(note)["hookSpecificOutput"]
    assert out["hookEventName"] == "UserPromptSubmit"
    assert "74% full (191k of 258k)" in out["additionalContext"]


def test_gemini_is_counted_to_where_it_compresses(tmp_path, monkeypatch, capsys):
    """Gemini compresses at half its window by default: 400k of 1M is 76%
    of the way there, and the note is JSON, since its plain stdout goes to
    the person, not the model."""
    _env(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    t = tmp_path / "session.jsonl"
    t.write_text("\n".join(json.dumps(r) for r in (
        {"sessionId": "g", "startTime": "x"},
        {"type": "user", "content": "hi"},
        {"type": "gemini", "model": "gemini-2.5-pro",
         "tokens": {"input": 400_000, "output": 10, "total": 400_010}},
    )) + "\n")
    out = _run(context.main, monkeypatch, capsys, {
        "transcript_path": str(t), "session_id": "g", "hook_event_name": "BeforeAgent"})
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "76% full (400k of 524k)" in ctx
