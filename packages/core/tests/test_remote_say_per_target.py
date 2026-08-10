"""Which lane a target renders down.

speech-dispatcher offers agent-media's targets as synthesis voices, so a client
can ask for `rooms`. That ask was cosmetic: one global MEDIA_REMOTE_SAY_CMD
served every target, so a reply addressed to the rooms was labelled rooms in
the history and heard wherever the global lane pointed — the phone.

The lane is now per target, with the global as the fallback that keeps
single-lane hosts working.
"""

import pytest

from agent_media_core.intake.submit import _remote_say_cmd
from agent_media_core.types import Target

CURL = "curl -sS --data-binary @- http://p8a:8790/say"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("MEDIA_REMOTE_SAY_CMD", "MEDIA_REMOTE_SAY_CMD_PHONE",
                "MEDIA_REMOTE_SAY_CMD_ROOMS", "MEDIA_REMOTE_SAY_CMD_LOCAL"):
        monkeypatch.delenv(key, raising=False)


def test_per_target_lane_wins(monkeypatch):
    monkeypatch.setenv("MEDIA_REMOTE_SAY_CMD", "global-lane")
    monkeypatch.setenv("MEDIA_REMOTE_SAY_CMD_PHONE", CURL)
    assert _remote_say_cmd(Target("phone")) == CURL


def test_global_lane_is_the_fallback(monkeypatch):
    """A host configured before per-target lanes existed must not change."""
    monkeypatch.setenv("MEDIA_REMOTE_SAY_CMD", CURL)
    assert _remote_say_cmd(Target("phone")) == CURL
    assert _remote_say_cmd(Target("rooms")) == CURL


def test_a_target_can_opt_out_of_the_global_lane(monkeypatch):
    """The whole point: `rooms` renders locally while `phone` keeps its lane.

    Empty is the off value here — an unset per-target key means "fall back to
    the global lane", so the two cannot be the same thing. In a config file
    this is written `-`; the env loader turns it into the empty string.
    """
    monkeypatch.setenv("MEDIA_REMOTE_SAY_CMD", CURL)
    monkeypatch.setenv("MEDIA_REMOTE_SAY_CMD_ROOMS", "")
    assert _remote_say_cmd(Target("rooms")) == ""      # local render+play
    assert _remote_say_cmd(Target("phone")) == CURL    # untouched


def test_no_lane_anywhere_is_local(monkeypatch):
    assert _remote_say_cmd(Target("local")) == ""


def test_target_names_are_normalised(monkeypatch):
    """Target names are logical ids that may contain dashes (matrix-room-x);
    _env_key upcases and underscores them, and this must agree with it."""
    monkeypatch.setenv("MEDIA_REMOTE_SAY_CMD_SNAPCAST_MEL", CURL)
    assert _remote_say_cmd(Target("snapcast-mel")) == CURL
