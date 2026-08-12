"""The off-host input claim: cece owns David's utterance, sam must not ask.

The contract that matters is the same one the rendezvous has — every unclear
path must read as UNCLAIMED. A claim that fails open costs an overlap, which is
the status quo; a claim that fails closed leaves converse permanently unable to
arm, which is a channel silent forever.
"""

import json
import time

import pytest

from agent_media_core.capture import input_claim
from agent_media_core.capture.rendezvous import (
    Busy, Claimed, Rendezvous, socket_path,
)


@pytest.fixture(autouse=True)
def _claim_path(tmp_path, monkeypatch):
    """Point both the claim file and the rendezvous socket at tmp_path."""
    monkeypatch.setattr(input_claim, "PATH", tmp_path / "input-claim.json")
    monkeypatch.setenv("MEDIA_CONVERSE_SOCK", str(tmp_path / "converse.sock"))
    monkeypatch.delenv("MEDIA_INPUT_CLAIM", raising=False)
    # The mirror is a subprocess to the relay; never spawn one from a test.
    monkeypatch.setattr("agent_media_core.capture.rendezvous._mirror",
                        lambda *a, **k: None)
    yield


def test_no_file_is_unclaimed():
    assert input_claim.held() is None


def test_a_fresh_claim_is_held():
    input_claim.claim("cece", ttl_s=60, source="phone-mic")
    cur = input_claim.held()
    assert cur is not None
    assert cur["owner"] == "cece"
    assert cur["source"] == "phone-mic"
    assert cur["age_s"] < 5


def test_a_claim_past_its_ttl_is_not_held():
    input_claim.claim("cece", ttl_s=0.05)
    time.sleep(0.1)
    assert input_claim.held() is None


def test_unparseable_claim_fails_open():
    input_claim.PATH.write_text("{not json")
    assert input_claim.held() is None


def test_claim_without_an_owner_fails_open():
    input_claim.PATH.write_text(json.dumps({"at": time.time(), "ttl_s": 60}))
    assert input_claim.held() is None


def test_a_clock_ahead_of_ours_still_reads_as_held():
    """Skew between two tailnet hosts is not evidence nobody is talking."""
    input_claim.PATH.write_text(json.dumps(
        {"owner": "cece", "at": time.time() + 30, "ttl_s": 60}))
    assert input_claim.held() is not None


def test_release_clears_it():
    input_claim.claim("cece", ttl_s=60)
    input_claim.release()
    assert input_claim.held() is None


def test_release_by_a_different_owner_is_ignored():
    """A late release from a finished session must not free a newer claim."""
    input_claim.claim("cece", ttl_s=60)
    input_claim.release("gigi")
    assert input_claim.held() is not None


def test_reclaiming_overwrites_rather_than_stacking():
    input_claim.claim("cece", ttl_s=60)
    input_claim.claim("gigi", ttl_s=60)
    assert input_claim.held()["owner"] == "gigi"


# --- the exclusion the whole thing exists for --------------------------------


def test_converse_refuses_to_arm_while_someone_owns_the_input():
    input_claim.claim("cece", ttl_s=60, source="phone-mic")
    with pytest.raises(Claimed) as e:
        with Rendezvous(timeout_s=1, question="q"):
            pass
    assert "cece" in str(e.value)


def test_claimed_is_catchable_as_busy():
    """Existing `except Busy` callers already back off correctly."""
    input_claim.claim("cece", ttl_s=60)
    with pytest.raises(Busy):
        with Rendezvous(timeout_s=1, question="q"):
            pass


def test_refusing_leaves_no_socket_behind():
    """A refused arm must not look like a stale rendezvous to the next caller."""
    input_claim.claim("cece", ttl_s=60)
    with pytest.raises(Claimed):
        with Rendezvous(timeout_s=1, question="q"):
            pass
    assert not socket_path().exists()


def test_converse_arms_once_the_claim_expires():
    input_claim.claim("cece", ttl_s=0.05)
    time.sleep(0.1)
    with Rendezvous(timeout_s=1, question="q"):
        assert socket_path().exists()


def test_the_kill_switch_ignores_the_claim(monkeypatch):
    monkeypatch.setenv("MEDIA_INPUT_CLAIM", "0")
    input_claim.claim("cece", ttl_s=60)
    with Rendezvous(timeout_s=1, question="q"):
        assert socket_path().exists()
