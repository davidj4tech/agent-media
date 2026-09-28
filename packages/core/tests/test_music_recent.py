"""Recently played (music_recent.py): the Media tab's way back through what
was played, newest first, with the conversation that put each on."""

from __future__ import annotations

import pytest

from agent_media_core import media_meta, music_recent
from agent_media_core.state import StateStore


@pytest.fixture()
def st(tmp_path, monkeypatch):
    monkeypatch.setattr(music_recent, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(music_recent, "_oembed", lambda vid: f"Named {vid}")
    monkeypatch.setattr(music_recent, "_cached_title", lambda vid: "")
    monkeypatch.setattr(media_meta, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(media_meta, "fill", lambda wanted: None)
    monkeypatch.setattr(music_recent, "_abs_by_file", lambda: {})
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


def test_a_youtube_play_has_its_picture(st):
    st.add_history(sink="music", uri="yt:https://www.youtube.com/watch?v=AAAAAAAAAAA", started_at=1)
    st.add_history(sink="music", uri="https://example.com/a.mp3", started_at=2)
    got = {r["uri"]: r["art"] for r in music_recent.recent(store=st)}
    assert got == {"yt:https://www.youtube.com/watch?v=AAAAAAAAAAA": "https://i.ytimg.com/vi/AAAAAAAAAAA/hqdefault.jpg",
                   "https://example.com/a.mp3": None}


def test_player_rows_do_not_crowd_out_older_plays(st):
    """A player writes a row per file it loads; hundreds of those must not
    push an older request out of the scan."""
    st.add_history(sink="music", uri="yt:https://www.youtube.com/watch?v=AAAAAAAAAAA", started_at=1)
    for i in range(500):
        st.add_history(sink="music", uri=f"/sdcard/cache/{i}.mka", started_at=10 + i)
    assert [r["uri"][-11:] for r in music_recent.recent(store=st)] == ["AAAAAAAAAAA"]


def test_a_track_says_what_its_title_says_until_looked_up(st):
    st.add_history(sink="music", uri="yt:https://www.youtube.com/watch?v=AAAAAAAAAAA",
                   started_at=1, text="Sunshower — Chris Cornell - Topic")
    got = music_recent.recent(store=st)[0]
    assert (got["kind"], got["artist"], got["song"], got["album"]) == ("music", "Chris Cornell", "Sunshower", None)
    media_meta._save({"yt:AAAAAAAAAAA": {"artist": "Chris Cornell", "song": "Sunshower",
                                          "album": "Songbook", "genre": "rock", "found": True}})
    got = music_recent.recent(store=st)[0]
    assert (got["album"], got["genre"]) == ("Songbook", "rock")


@pytest.mark.parametrize("title,channel,want", [
    ("Sunshower", "Chris Cornell - Topic", ("Chris Cornell", "Sunshower", True)),
    ("O.A.R. - Crazy Game of Poker (STUDIO VERSION) w/ Lyrics in Description!!!", "popgarbage94",
     ("O.A.R.", "Crazy Game of Poker (STUDIO VERSION)", True)),
    ("Blackjack (2005 Remaster)", "", (None, "Blackjack", False)),
    ("Spiritual Morning Mix | Yoga", "Some Channel", ("Some Channel", "Spiritual Morning Mix", False)),
])
def test_split_title(title, channel, want):
    assert media_meta.split_title(title, channel) == want


@pytest.mark.parametrize("title,want", [
    ("Hounded: The Iron Druid Chronicles, Book 1 (Unabridged)", "The Iron Druid Chronicles"),
    ("The Great Big Bear and Other Stories of the Iron Druid Chronicles", "The Iron Druid Chronicles"),
    ("The Dark Age: The Ancient Future Trilogy, Book 1 (Unabridged)", "The Ancient Future Trilogy"),
    ("Blood Scent (Unabridged)", None),
])
def test_series_from_a_title(title, want):
    assert media_meta.series_of(title) == want


def test_books_and_podcasts_are_their_own_lists(st, monkeypatch):
    item = {"id": "i1", "relPath": "Hounded.m4b", "media": {"metadata": {
        "title": "Hounded: The Iron Druid Chronicles, Book 1 (Unabridged)", "authorName": "Kevin Hearne",
        "genres": ["Audiobook"], "narratorName": "Luke Daniels"}}}
    monkeypatch.setattr(music_recent, "_abs_by_file", lambda: {"Hounded.m4b": item, "id:i1": item})
    st.add_history(sink="book", uri="/home/u/audiobooks/Hounded.m4b", started_at=1)
    st.add_history(sink="book", uri="https://traffic.libsyn.com/show/ep205.mp3", started_at=2,
                   extras={"title": "Episode 205"})
    st.add_history(sink="book", uri="/home/u/conversations/p-x/reply.mp3", started_at=3)
    st.add_history(sink="book", uri="http://127.0.0.1:13378/api/items/0000/download?token=secret", started_at=4)
    books = music_recent.recent(store=st, kind="book")
    assert [(b["title"][:7], b["author"], b["series"], b["narrator"], b["genre"]) for b in books] == \
        [("Hounded", "Kevin Hearne", "The Iron Druid Chronicles", "Luke Daniels", None)]
    pods = music_recent.recent(store=st, kind="podcast")
    assert [(p["title"], p["author"]) for p in pods] == [("Episode 205", "traffic.libsyn.com")]
    assert not any("token" in r["uri"] for r in books + pods)
