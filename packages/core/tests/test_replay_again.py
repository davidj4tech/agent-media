"""`media replay --again`: a headset's play with nothing loaded on the phone.

David, 16 Oct 2026: "can tapping the earbud play button be the same as
tapping the most recent play button in a thread if there's nothing else to
play?" — and, asked which: "latest in an open thread if the last replay has
already finished... otherwise resume the last replay".
"""

import json
import time

import pytest

from agent_media_core import cli, watching
from agent_media_core.state.store import StateStore


# ---- what spoke last ------------------------------------------------------

def test_the_last_reply_outlives_its_now_playing_row(tmp_path):
    st = StateStore(tmp_path / "state.db")
    assert st.last_speech() == {}
    st.set_now_playing("speech", "/c/a.mp3", 100.0, extras={"history_id": 7})
    st.clear_now_playing("speech")
    assert st.last_speech() == {"started_at": 100.0, "history_id": 7}
    # A live reply has no row yet: it is found by its start.
    st.set_now_playing("speech", "/c/b.mp3", 200.0, extras={"text": "hi"})
    assert st.last_speech() == {"started_at": 200.0, "history_id": None}
    # Music is not speech.
    st.set_now_playing("music", "file:///x.mp3", 300.0)
    assert st.last_speech()["started_at"] == 200.0


# ---- the open threads -----------------------------------------------------

def test_open_sessions_leased_newest_first_then_on_screen(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    d = tmp_path / "agent-media"
    d.mkdir()
    now = time.time()
    (d / watching.POCKET_NAME).write_text(json.dumps(
        {"old": now + 100, "new": now + 900, "gone": now - 5}))
    import os
    (d / watching.NAME).write_text(json.dumps(
        {"pid": os.getpid(), "sessions": {"seen": 1, "new": 1, "closed": 0}}))
    assert watching.open_sessions() == ["new", "old", "seen"]


def test_open_sessions_none(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert watching.open_sessions() == []


# ---- the choice -----------------------------------------------------------

@pytest.fixture
def rig(monkeypatch):
    rec = {"played": [], "last": None, "resume": None, "open": [],
           "threads": {"t-open": [{"id": 9}]}}
    monkeypatch.setattr(cli, "_last_spoken_row", lambda: rec["last"])
    monkeypatch.setattr(cli, "_question_before", lambda row: None)
    monkeypatch.setattr(cli, "_resume_point", lambda row, q: rec["resume"])
    monkeypatch.setattr(cli, "_replay_row", lambda row, from_sentence=None, then_id=None:
                        rec["played"].append(("row", row["id"], from_sentence)) or 0)
    monkeypatch.setattr(cli, "_do_replay", lambda index, session=None:
                        rec["played"].append(("newest", session)) or 0)
    monkeypatch.setattr(cli, "_speech_history", lambda n=20, session=None, include_live=False:
                        rec["threads"].get(session, []))
    monkeypatch.setattr(watching, "open_sessions", lambda: rec["open"])
    return rec


def test_a_reply_stopped_part_way_resumes(rig):
    rig["last"] = {"id": 4}
    rig["resume"] = 3
    rig["open"] = ["t-open"]
    assert cli._replay_again() == 0
    assert rig["played"] == [("row", 4, 3)]


def test_a_finished_reply_gives_way_to_the_open_thread(rig):
    rig["last"] = {"id": 4}
    rig["open"] = ["t-empty", "t-open"]
    assert cli._replay_again() == 0
    assert rig["played"] == [("newest", "t-open")]


def test_no_open_thread_plays_the_newest_of_all(rig):
    assert cli._replay_again() == 0
    assert rig["played"] == [("newest", None)]


def test_the_flag_reaches_it(rig):
    assert cli.main(["replay", "--again"]) == 0
    assert rig["played"] == [("newest", None)]
