"""Beat images may use their own (faster) engine than single images."""

from agent_media_visual import cli


def test_explicit_cli_engine_wins(monkeypatch):
    monkeypatch.setenv("MEDIA_VISUAL_BEATS_ENGINE", "venice")
    assert cli._beats_engine("svg") == "svg"


def test_env_beats_engine_used(monkeypatch):
    monkeypatch.setenv("MEDIA_VISUAL_BEATS_ENGINE", "venice")
    assert cli._beats_engine(None) == "venice"


def test_unset_falls_through_to_normal_resolution(monkeypatch):
    monkeypatch.delenv("MEDIA_VISUAL_BEATS_ENGINE", raising=False)
    assert cli._beats_engine(None) is None
