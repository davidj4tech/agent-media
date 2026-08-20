"""A dictation hold that fires far too often to be dictation.

The hold pauses Sam while David talks to his keyboard, and it cannot tell him
from the phone's own recogniser by anything it can see. On p8a that recogniser
holds the mic for ten seconds at a time whenever com.google.android.as is not
blocked from RECORD_AUDIO, and the block reverts by itself. Twice now that
arrived as "TTS keeps pausing", with every component healthy — so the rate is
the fact that has to reach `media doctor`.
"""

import json
from unittest import mock

from agent_media_core import cli


def _state(payload):
    """urlopen standing in for the companion's status server."""
    class _Resp:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return mock.patch("urllib.request.urlopen", return_value=_Resp())


def test_a_healthy_phone_reports_the_rate_and_no_problem():
    with _state({"dictation_holds_1h": 3}):
        facts = cli._dictation_rate_facts()
    assert facts == {"dictation_holds_1h": "3"}
    assert cli.health_problems(facts) == []


def test_the_apps_complaint_becomes_the_doctors():
    """The app owns the threshold — it is the thing that can see the rate —
    so doctor carries its words rather than re-deciding them."""
    said = ("the dictation hold engaged 47 times in the last hour — far more "
            "than dictation, so speech is being paused by the phone's own "
            "recogniser holding the mic. Check whether com.google.android.as "
            "is blocked from RECORD_AUDIO; the block reverts on its own.")
    with _state({"dictation_holds_1h": 47, "dictation_rate": said}):
        facts = cli._dictation_rate_facts()
    assert facts["dictation_holds_1h"] == "47"
    assert said in cli.health_problems(facts)


def test_a_host_without_the_companion_says_nothing():
    """Not a zero: every non-phone host in the fleet runs this, and a fact
    they cannot know must not read as a healthy answer."""
    with mock.patch("urllib.request.urlopen", side_effect=OSError("refused")):
        assert cli._dictation_rate_facts() == {}


def test_a_companion_answering_rubbish_is_not_a_fact():
    with _state(["not", "a", "dict"]):
        assert cli._dictation_rate_facts() == {}
    with _state({"dictation_holds_1h": "lots"}):
        assert cli._dictation_rate_facts() == {}
