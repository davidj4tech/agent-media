""""Read from here" (server-contract.md §6.5): a tap on a sentence of the
message being said jumps the voice there (`goto-sentence`); on an older
reply, `replay-id` + `sentence` plays it from one, and `/speech/sentences`
names the sentences that index counts.

Nothing here reaches a player or the live canvas: `canvas._media` and
`canvas._media_ctl` are recorders, the speech snapshot is faked, and the
history rows are written to the test's own state dir (conftest).
"""

from __future__ import annotations

import pytest

from agent_media_core.state.store import StateStore
from agent_media_visual import canvas

from test_contract import AUTH, SID, SID2, call, server, shelf, signed_in, typed  # noqa: F401


@pytest.fixture()
def speaking(monkeypatch):
    """The canvas says SID's reply is being said."""
    state = {"kind": "state", "speaking": True, "session": SID}
    monkeypatch.setattr(canvas, "speech_state", lambda: dict(state))
    return state


@pytest.fixture()
def replays(monkeypatch):
    ran: list = []

    def rec(argv, timeout):
        ran.append(argv)
        return ""

    monkeypatch.setattr(canvas, "_media_ctl", rec)
    return ran


def _skips(typed):
    return [a[0] for n, a in typed if n == "_media" and a and a[0][:1] == ["skip"]]


# --- goto-sentence ----------------------------------------------------------------

def test_goto_sentence_jumps_the_reply_being_said(server, shelf, signed_in, typed, speaking):
    res, obj = call(server, "POST", "/speech/ctl",
                    {"action": "goto-sentence", "arg": 3, "session": SID}, AUTH)
    assert res.status == 200 and obj["ok"] is True
    argv = _skips(typed)[-1]
    assert argv[:5] == ["skip", "--unit", "sentence", "--to", "3"]


def test_sentence_zero_is_a_sentence(server, shelf, signed_in, typed, speaking):
    res, _ = call(server, "POST", "/speech/ctl", {"action": "goto-sentence", "arg": 0}, AUTH)
    assert res.status == 200
    assert _skips(typed)[-1][3:5] == ["--to", "0"]


@pytest.mark.parametrize("arg", [True, "3", -1, 10000, 2.5, None])
def test_goto_sentence_takes_only_an_index(server, shelf, signed_in, typed, speaking, arg):
    body = {"action": "goto-sentence"}
    if arg is not None:
        body["arg"] = arg
    res, obj = call(server, "POST", "/speech/ctl", body, AUTH)
    assert res.status == 400
    assert obj == {"ok": False, "error": "arg must be a sentence index"}
    assert _skips(typed) == []


def test_goto_sentence_never_brings_a_finished_reply_back(server, shelf, signed_in, typed,
                                                          speaking):
    speaking["speaking"] = False
    res, obj = call(server, "POST", "/speech/ctl", {"action": "goto-sentence", "arg": 2}, AUTH)
    assert res.status == 409 and obj == {"ok": False, "error": "nothing is being said"}
    assert _skips(typed) == []


def test_goto_sentence_works_while_paused(server, shelf, signed_in, typed, speaking):
    speaking.update(speaking=False, paused=True)
    res, _ = call(server, "POST", "/speech/ctl", {"action": "goto-sentence", "arg": 2}, AUTH)
    assert res.status == 200
    assert _skips(typed)


def test_goto_sentence_in_another_thread_is_refused(server, shelf, signed_in, typed, speaking):
    res, obj = call(server, "POST", "/speech/ctl",
                    {"action": "goto-sentence", "arg": 2, "session": SID2}, AUTH)
    assert res.status == 409
    assert obj["error"] == "that reply is no longer being said"
    assert _skips(typed) == []


def test_goto_sentence_needs_the_listener(server, shelf, typed, speaking):
    res, obj = call(server, "POST", "/speech/ctl", {"action": "goto-sentence", "arg": 1})
    assert res.status in (401, 403) and obj["ok"] is False
    assert _skips(typed) == []


# --- replay-id from a sentence ------------------------------------------------------

def test_replay_id_from_a_sentence_is_one_command(server, shelf, signed_in, replays):
    res, obj = call(server, "POST", "/speech/ctl",
                    {"action": "replay-id", "arg": 48213, "sentence": 4}, AUTH)
    assert res.status == 200 and obj["ok"] is True
    assert replays == [["replay", "--id", "48213", "--from-sentence", "4"]]


def test_replay_id_without_a_sentence_is_unchanged(server, shelf, signed_in, replays):
    call(server, "POST", "/speech/ctl", {"action": "replay-id", "arg": 48213}, AUTH)
    assert replays == [["replay", "--id", "48213"]]


@pytest.mark.parametrize("bad", [True, "4", -1, 1.5])
def test_replay_id_sentence_is_validated(server, shelf, signed_in, replays, bad):
    res, obj = call(server, "POST", "/speech/ctl",
                    {"action": "replay-id", "arg": 48213, "sentence": bad}, AUTH)
    assert res.status == 400
    assert obj == {"ok": False, "error": "sentence must be a sentence index"}
    assert replays == []


# --- /speech/sentences ----------------------------------------------------------------

def _spoken_row(**extras) -> int:
    return StateStore().add_history(sink="speech", uri="/c/0.mp3", started_at=1.0,
                                    text="One. Two. Three.", extras=extras)


def test_sentences_are_the_ones_the_replay_counts(server, shelf, signed_in):
    rid = _spoken_row(clip_uris=["/c/0.mp3", "/c/1.mp3", "/c/2.mp3"],
                      clip_sentences=["One.", "Two.", "Three."])
    res, obj = call(server, "GET", f"/speech/sentences?id={rid}", None, AUTH)
    assert res.status == 200
    assert obj == {"ok": True, "id": rid, "sentences": ["One.", "Two.", "Three."]}


def test_a_reply_with_no_timeline_has_no_sentences_to_start_at(server, shelf, signed_in):
    rid = _spoken_row(clip_uris=["/c/all.mp3"], clip_sentences=["One.", "Two."])
    res, obj = call(server, "GET", f"/speech/sentences?id={rid}", None, AUTH)
    assert res.status == 200 and obj["sentences"] == []


def test_sentences_of_a_row_that_is_not_speech(server, shelf, signed_in):
    rid = StateStore().add_history(sink="music", uri="x", started_at=1.0)
    res, obj = call(server, "GET", f"/speech/sentences?id={rid}", None, AUTH)
    assert res.status == 404 and obj == {"ok": False, "error": "no such spoken reply"}
    res, obj = call(server, "GET", "/speech/sentences?id=99999", None, AUTH)
    assert res.status == 404


def test_sentences_needs_a_number_and_the_listener(server, shelf, signed_in):
    res, obj = call(server, "GET", "/speech/sentences?id=abc", None, AUTH)
    assert res.status == 400 and obj["ok"] is False


def test_sentences_is_gated(server, shelf):
    res, obj = call(server, "GET", "/speech/sentences?id=1")
    assert res.status in (401, 403) and obj["ok"] is False
