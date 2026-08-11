"""The rows' text has to keep pace with the audio."""
from __future__ import annotations
import pytest
from agent_media_core.intake import submit


@pytest.fixture
def tmux(monkeypatch):
    """Record the tmux argv instead of running it."""
    calls: list = []

    class _R:
        returncode = 0
        stdout = "sess\t54"

    def _run(argv, **kw):
        calls.append(argv)
        return _R()

    monkeypatch.setenv("TMUX", "x")
    monkeypatch.setenv("MEDIA_FOLLOW_ROWS", "4")
    monkeypatch.setattr(submit, "_is_auto_highlight_enabled", lambda: True)
    monkeypatch.setattr(submit.subprocess, "run", _run)
    return calls


def _rows(calls):
    """The four @am_follow_N values from the one batched tmux invocation."""
    argv = [c for c in calls if "set" in c][0]
    out = []
    for i in range(4):
        out.append(argv[argv.index(f"@am_follow_{i}") + 1])
    return out


def test_the_sentence_is_published_as_options_not_shelled_out_for(tmux):
    """A #() in status-format runs once per status-interval and caches — 1-2s
    late, measured. An option is read at draw time."""
    submit.publish_follow_text("A short sentence.", "%1")
    assert _rows(tmux)[0] == "♪ A short sentence."
    assert any("refresh-client" in c for c in tmux), "the bar was never redrawn"


def test_a_long_sentence_wraps_across_the_rows(tmux):
    submit.publish_follow_text(" ".join(["word"] * 60), "%1")
    rows = _rows(tmux)
    assert all(len(r) <= 54 for r in rows)
    assert rows[1].startswith("  "), "continuation rows are indented"
    assert rows[3].endswith("…"), "the last row should say it was cut"


def test_whitespace_is_collapsed(tmux):
    submit.publish_follow_text("Two   lines\nof  it.", "%1")
    assert _rows(tmux)[0] == "♪ Two lines of it."


def test_idle_says_the_feature_is_on(tmux):
    submit.publish_follow_text(None, "%1")
    rows = _rows(tmux)
    assert "follow-along on" in rows[0]
    assert rows[1:] == ["", "", ""]


def test_silent_while_the_feature_is_off(tmux, monkeypatch):
    monkeypatch.setattr(submit, "_is_auto_highlight_enabled", lambda: False)
    submit.publish_follow_text("Something being read.", "%1")
    assert _rows(tmux) == ["", "", "", ""]


def test_no_tmux_no_publish(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    calls: list = []
    monkeypatch.setattr(submit.subprocess, "run", lambda *a, **k: calls.append(a))
    submit.publish_follow_text("x", "%1")
    assert calls == []


def test_every_lane_publishes_through_the_scheduler(monkeypatch):
    """One choke point: live local, live phone and replay all call show()."""
    seen: list = []
    monkeypatch.setattr(submit, "publish_follow_text",
                        lambda s, pane="": seen.append(s))
    monkeypatch.setattr(submit, "_tmux_highlight_text", lambda *a, **k: True)
    monkeypatch.setattr(submit, "_set_follow_rows", lambda *a, **k: None)
    h = submit._HighlightScheduler(0.0, True, "%1")
    h.show("The sentence.", first=True, force=False)
    h.drain()
    assert seen[0] == "The sentence."
    assert seen[-1] is None, "the rows were not returned to the idle hint"
