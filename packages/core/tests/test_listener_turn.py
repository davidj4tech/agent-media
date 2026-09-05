"""The listener's typed reply becomes a turn in the conversation."""
import json
from pathlib import Path

from agent_media_core import book_tracks


def test_render_failure_records_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(book_tracks, "render_text", None, raising=False)
    import agent_media_core.render.engines as eng
    monkeypatch.setattr(eng, "render_text", lambda *a, **k: (False, "no engine"))
    assert book_tracks.record_listener_turn("s1", "hello") is False


def test_empty_text_is_not_a_turn():
    assert book_tracks.record_listener_turn("s1", "   ") is False
    assert book_tracks.record_listener_turn("", "hello") is False
