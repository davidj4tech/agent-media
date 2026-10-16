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
