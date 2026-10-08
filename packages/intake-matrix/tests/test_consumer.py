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


# --- what a message does: the room's speech level decides -----------------

import time  # noqa: E402

OWNER, SAM, MEL = "@david:ryer.org", "@sam:ryer.org", "@mel:ryer.org"


class Store:
    def __init__(self):
        self.rows = []

    def add_history(self, **kw):
        self.rows.append(kw)
        return len(self.rows)


@pytest.fixture
def said(monkeypatch):
    got = []
    monkeypatch.setattr(intake, "_speak_text",
                        lambda text, *, decision, extras, state: got.append((text, decision, extras)))
    monkeypatch.setattr(intake.matrix, "download", lambda url, token, dest: True)
    return got


def _msg(sender, body="hi", msgtype="m.text", age=0, **content):
    return {"type": "m.room.message", "event_id": "$e", "sender": sender,
            "origin_server_ts": int((time.time() - age) * 1000),
            "content": {"msgtype": msgtype, "body": body, **content}}


def _process(ev, decision="hold", store=None, monkeypatch=None):
    monkeypatch.setattr(intake, "_decide", lambda thread: decision)
    sink = type("S", (), {"pause": lambda *a: None})()
    return intake._process_event(
        ev, room_id=ROOM, sam_id=SAM, control_ids={OWNER, SAM},
        homeserver="https://hs.test", token="t", sink=sink, music=sink,
        coordinator=None, state=store or Store(), target=intake.Target(name="local"),
        owner=OWNER, name_of=lambda room, user: {MEL: "Mel"}.get(user, ""))


def test_someone_elses_text_follows_the_rooms_level(said, monkeypatch):
    assert _process(_msg(MEL, "on my way"), "hold", monkeypatch=monkeypatch)
    (text, decision, extras), = said
    assert text == "Mel: on my way" and decision == "hold"
    assert extras == {"session": intake.matrix.thread_of(ROOM), "matrix_event": "$e",
                      "matrix_room": ROOM}


def test_the_owners_words_are_a_command_or_nothing(said, monkeypatch):
    assert not _process(_msg(OWNER, "see you soon"), monkeypatch=monkeypatch)
    assert _process(_msg(OWNER, "pause"), monkeypatch=monkeypatch)
    assert said == []


def test_an_old_message_or_an_edit_is_not_read(said, monkeypatch):
    assert not _process(_msg(MEL, age=3600), monkeypatch=monkeypatch)
    assert not _process(_msg(MEL, **{"m.relates_to": {"rel_type": "m.replace"}}),
                        monkeypatch=monkeypatch)
    assert said == []


def test_a_held_voice_note_is_kept_unplayed(said, monkeypatch):
    store = Store()
    assert _process(_msg(SAM, "voice.ogg", "m.voice", url="mxc://hs/abc"), "hold",
                    store=store, monkeypatch=monkeypatch)
    (row,), = [store.rows]
    assert row["extras"]["held"] and row["extras"]["matrix_event"] == "$e"
    assert row["extras"]["session"] == intake.matrix.thread_of(ROOM)
