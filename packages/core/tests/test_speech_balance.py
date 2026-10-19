"""The speech and music balance (speech_balance.py): the app's two gains."""

from __future__ import annotations

import pytest

from agent_media_core import speech_balance as sb
from agent_media_core.sinks.speech import SinkSpeech
from agent_media_core.types import Target


@pytest.fixture(autouse=True)
def _state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for k in [k for k in __import__("os").environ if k.startswith("MEDIA_SPEECH_SOCKET")]:
        monkeypatch.delenv(k, raising=False)


def test_level_until_set_is_both_full():
    assert sb.balance() == 0
    assert (sb.speech_level(), sb.music_level()) == (100, 100)


@pytest.mark.parametrize("b, speech, music", [
    (30, 100, 70), (-40, 60, 100), (100, 100, 0), (-100, 0, 100)])
def test_the_louder_side_stays_full(b, speech, music):
    assert sb.set_balance(b) == b
    assert (sb.speech_level(), sb.music_level()) == (speech, music)


def test_saved_balance_is_clamped():
    assert sb.set_balance(250) == 100 and sb.balance() == 100
    assert sb.set_balance(-7.6) == -8


def test_a_reply_on_the_app_starts_at_the_speech_level():
    sb.set_balance(-25)
    cmds = SinkSpeech._start_cmds(0, Target(name="sasonica"))
    assert cmds[0] == ["set_property", "volume", 75.0]
    # Other speakers keep their own volume.
    other = SinkSpeech._start_cmds(0, Target(name="rooms"))
    assert not any(c[1] == "volume" for c in other)
    assert not any(c[1] == "volume" for c in SinkSpeech._start_cmds())
