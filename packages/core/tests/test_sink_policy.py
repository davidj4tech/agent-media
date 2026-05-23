"""Tests for 4C sink-naming-convention duck-vs-pause policy."""

import os

import pytest

from agent_media_core.route import policy
from agent_media_core.route.policy import (
    InterruptionStrategy,
    resolve_policy,
    strategy_for_sink,
)
from agent_media_core.types import ContentType


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("MEDIA_DUCKABLE_SINKS", raising=False)
    yield


def test_duckable_sinks_duck():
    assert strategy_for_sink("am") is InterruptionStrategy.DUCK
    assert strategy_for_sink("am-music") is InterruptionStrategy.DUCK


def test_other_named_sink_pauses():
    assert strategy_for_sink("default") is InterruptionStrategy.PAUSE
    assert strategy_for_sink("sp4r-hdmi") is InterruptionStrategy.PAUSE


def test_unknown_sink_defers():
    assert strategy_for_sink(None) is None


def test_duckable_sinks_env_override(monkeypatch):
    monkeypatch.setenv("MEDIA_DUCKABLE_SINKS", "am,am-music,aar,aar-music")
    assert strategy_for_sink("aar") is InterruptionStrategy.DUCK
    assert strategy_for_sink("am-music") is InterruptionStrategy.DUCK
    assert strategy_for_sink("default") is InterruptionStrategy.PAUSE


def test_empty_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("MEDIA_DUCKABLE_SINKS", "  ")
    assert policy.duckable_sinks() == policy.DUCKABLE_SINKS


def test_resolve_pauses_on_movie_sink():
    # Even music content forced to PAUSE when on a non-duckable sink.
    p = resolve_policy(ContentType.MUSIC, sink_name="default")
    assert p.strategy is InterruptionStrategy.PAUSE


def test_resolve_uses_content_type_on_duckable_sink():
    # Audiobook on a duckable sink still pauses (content type wins there).
    p = resolve_policy(ContentType.AUDIOBOOK, sink_name="am-music")
    assert p.strategy is InterruptionStrategy.PAUSE
    # Music on a duckable sink ducks.
    p = resolve_policy(ContentType.MUSIC, sink_name="am-music")
    assert p.strategy is InterruptionStrategy.DUCK


def test_resolve_unknown_sink_uses_content_type():
    # No sink info → behave exactly like the content-type policy.
    assert (resolve_policy(ContentType.MUSIC).strategy
            is policy.policy_for(ContentType.MUSIC).strategy)
    assert (resolve_policy(ContentType.PODCAST).strategy
            is InterruptionStrategy.PAUSE)
