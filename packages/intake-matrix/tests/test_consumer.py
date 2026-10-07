"""The speech intake as a reader of the shared loop: which rooms it plays.
Sinks are stubbed — nothing here may reach a real player."""

import threading

import pytest

import agent_media_intake_matrix as intake
from agent_media_core import matrix

ROOM = "!sam:ryer.org"
BRIDGED = "!sms:ryer.org"


@pytest.fixture
def handled(monkeypatch):
    got = []
    done = threading.Event()
    for name in ("SinkSpeech", "SinkMusic", "StateStore"):
        monkeypatch.setattr(intake, name, lambda *a, **k: object())
    monkeypatch.setattr(intake, "Coordinator", lambda *a, **k: object())

    def fake(ev, *, room_id, **_):
        got.append((room_id, ev["event_id"]))
        if ev["event_id"] == "$end":
            done.set()

    monkeypatch.setattr(intake, "_process_event", fake)
    return got, done


def _run(env, events):
    cfg = matrix.Config(homeserver="https://hs.test", token="t",
                        rooms={ROOM, BRIDGED})
    on_event, stop = intake.consumer(cfg, env)
    for room, ev_id in events:
        on_event(room, {"event_id": ev_id})
    on_event(ROOM, {"event_id": "$end"})
    return stop


def test_every_allowed_room_speaks_by_default(handled):
    got, done = handled
    stop = _run({}, [(ROOM, "$1"), (BRIDGED, "$2")])
    assert done.wait(5)
    stop()
    assert got[:2] == [(ROOM, "$1"), (BRIDGED, "$2")]


def test_a_room_left_out_of_speech_rooms_is_never_played(handled):
    got, done = handled
    stop = _run({"MATRIX_SPEECH_ROOMS": ROOM}, [(BRIDGED, "$2"), (ROOM, "$1")])
    assert done.wait(5)
    stop()
    assert got == [(ROOM, "$1"), (ROOM, "$end")]
