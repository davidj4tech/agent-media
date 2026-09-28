"""Recently played (music_recent.py): the Media tab's way back through what
was played, newest first, with the conversation that put each on."""

from __future__ import annotations

import pytest

from agent_media_core import music_recent
from agent_media_core.state import StateStore


@pytest.fixture()
def st(tmp_path, monkeypatch):
    monkeypatch.setattr(music_recent, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(music_recent, "_oembed", lambda vid: f"Named {vid}")
    monkeypatch.setattr(music_recent, "_cached_title", lambda vid: "")
    for k in ("MEDIA_SOURCE_SESSION", "CLAUDE_CODE_SESSION_ID", "MEDIA_SESSIOND_SESSION"):
        monkeypatch.delenv(k, raising=False)
    return StateStore(tmp_path / "state.db")


def _speech(st, at, sid):
    st.add_history(sink="speech", uri="/x.mp3", started_at=at, text="hi",
                   extras={"source_session": sid})


def test_a_play_records_its_conversation(st, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-a")
    st.note_play("music", "yt:https://www.youtube.com/watch?v=AAAAAAAAAAA", title="A song")
    got = music_recent.recent(store=st)
    assert [(r["title"], r["session"], r["inferred"]) for r in got] == [("A song", "sess-a", False)]


def test_newest_first_one_per_video_players_rows_left_out(st):
    st.add_history(sink="music", uri="https://www.youtube.com/watch?v=AAAAAAAAAAA", started_at=100)
    st.add_history(sink="music", uri="yt:https://www.youtube.com/watch?v=BBBBBBBBBBB", started_at=200, text="B")
    st.add_history(sink="music", uri="http://localhost:6616/BBBBBBBBBBB.mka", started_at=201,
                   extras={"interruption": {"strategy": "duck"}})
    st.add_history(sink="music", uri="yt:https://www.youtube.com/watch?v=AAAAAAAAAAA", started_at=300)
    got = music_recent.recent(store=st)
    assert [r["uri"] for r in got] == ["yt:https://www.youtube.com/watch?v=AAAAAAAAAAA",
                                       "yt:https://www.youtube.com/watch?v=BBBBBBBBBBB"]
    assert [r["title"] for r in got] == ["Named AAAAAAAAAAA", "B"]


def test_an_old_play_is_given_the_conversation_that_spoke_just_before(st):
    _speech(st, 1000, "earlier")
    _speech(st, 1090, "asker")
    st.add_history(sink="music", uri="yt:https://www.youtube.com/watch?v=AAAAAAAAAAA", started_at=1100)
    _speech(st, 1110, "after")
    st.add_history(sink="music", uri="yt:https://www.youtube.com/watch?v=BBBBBBBBBBB", started_at=5000)
    got = {r["uri"][-11:]: (r["session"], r["inferred"]) for r in music_recent.recent(store=st)}
    assert got == {"AAAAAAAAAAA": ("asker", True), "BBBBBBBBBBB": (None, False)}


def test_names_are_looked_up_once(st, monkeypatch):
    calls = []
    monkeypatch.setattr(music_recent, "_oembed", lambda vid: calls.append(vid) or "N")
    st.add_history(sink="music", uri="yt:https://www.youtube.com/watch?v=AAAAAAAAAAA", started_at=1)
    music_recent.recent(store=st)
    music_recent.recent(store=st)
    assert calls == ["AAAAAAAAAAA"]
