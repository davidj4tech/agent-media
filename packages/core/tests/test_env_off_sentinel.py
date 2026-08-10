"""Turning a setting off for one invocation, without editing the config file.

The loader backfills a present-but-empty var, which is right for a login shell
exporting `OPENAI_API_KEY=''` and wrong for `MEDIA_REMOTE_SAY_CMD= media say`:
the switch was refilled from agent-media.env, so it could not be turned off at
all. Nothing distinguishes the two empties, so `-` says which one is meant.

Both directions are asserted here, because a fix for one that quietly breaks
the other is how the 401 in 7c273a2 happened in the first place.
"""

import os

import pytest

from agent_media_core.intake import _env
from agent_media_core.intake._env import load_env_file


@pytest.fixture(autouse=True)
def _reset_off_keys():
    """The off-set is deliberately process-wide, so it outlives a test too —
    without this, the first test to switch a key off silently disables it for
    every test that runs afterwards in the same process."""
    _env._OFF_KEYS.clear()
    yield
    _env._OFF_KEYS.clear()


def _env_file(tmp_path, body: str):
    path = tmp_path / "agent-media.env"
    path.write_text(body)
    return path


def test_off_sentinel_survives_the_config_file(tmp_path, monkeypatch):
    env = _env_file(tmp_path, "MEDIA_REMOTE_SAY_CMD=curl -sS http://p8a:8790/say\n")
    monkeypatch.setenv("MEDIA_ENV_FILE", str(env))
    monkeypatch.setenv("MEDIA_REMOTE_SAY_CMD", "-")

    load_env_file("test")

    # Empty, not "-": every consumer gates on truthiness (`if remote_say:`),
    # so the off switch has to read as off without any of them knowing.
    assert os.environ["MEDIA_REMOTE_SAY_CMD"] == ""


def test_empty_is_still_backfilled(tmp_path, monkeypatch):
    """7c273a2's case, unchanged: an empty secret must not block the real one."""
    env = _env_file(tmp_path, "OPENAI_API_KEY=sk-real\n")
    monkeypatch.setenv("MEDIA_ENV_FILE", str(env))
    monkeypatch.setenv("OPENAI_API_KEY", "")

    load_env_file("test")

    assert os.environ["OPENAI_API_KEY"] == "sk-real"


def test_a_lower_layer_cannot_undo_the_decision(tmp_path, monkeypatch):
    """The sentinel is consumed by the first file that mentions the key, which
    would leave it merely empty — and empty is what every later layer fills."""
    high = _env_file(tmp_path, "MEDIA_REMOTE_SAY_CMD=from-high\n")
    low = tmp_path / "agent-audio-relay.env"
    low.write_text("MEDIA_REMOTE_SAY_CMD=from-low\n")
    monkeypatch.setenv("MEDIA_ENV_FILE", str(high))
    monkeypatch.setenv("RELAY_ENV_FILE", str(low))
    monkeypatch.setenv("MEDIA_REMOTE_SAY_CMD", "-")

    load_env_file("test")

    assert os.environ["MEDIA_REMOTE_SAY_CMD"] == ""


def test_an_unrelated_dash_valued_var_is_left_alone(tmp_path, monkeypatch):
    """Only keys named by an env file are considered, so a var whose real value
    is legitimately `-` is never rewritten."""
    env = _env_file(tmp_path, "MEDIA_SPEECH_DEFAULT_TARGET=phone\n")
    monkeypatch.setenv("MEDIA_ENV_FILE", str(env))
    monkeypatch.setenv("SOME_OTHER_TOOL_INPUT", "-")

    load_env_file("test")

    assert os.environ["SOME_OTHER_TOOL_INPUT"] == "-"


def test_a_second_load_does_not_refill_it(tmp_path, monkeypatch):
    """cli.py loads at import and again in main(). By the second call the
    sentinel is already an empty string — which is exactly what a load
    backfills — so the decision has to outlive the call that made it."""
    env = _env_file(tmp_path, "MEDIA_REMOTE_SAY_CMD=curl -sS http://p8a:8790/say\n")
    monkeypatch.setenv("MEDIA_ENV_FILE", str(env))
    monkeypatch.setenv("MEDIA_REMOTE_SAY_CMD", "-")

    load_env_file("first")
    load_env_file("second")

    assert os.environ["MEDIA_REMOTE_SAY_CMD"] == ""


def test_normal_layering_is_unchanged(tmp_path, monkeypatch):
    env = _env_file(tmp_path, "MEDIA_SPEECH_DEFAULT_TARGET=phone\n")
    monkeypatch.setenv("MEDIA_ENV_FILE", str(env))
    monkeypatch.setenv("MEDIA_SPEECH_DEFAULT_TARGET", "rooms")

    load_env_file("test")

    assert os.environ["MEDIA_SPEECH_DEFAULT_TARGET"] == "rooms"
