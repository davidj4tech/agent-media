"""Play history for the music and book channels.

The `history` table always had a `sink` column and only speech ever wrote to
it. These are the rows for the other two channels — one per item deliberately
put on — and the memory that fixes music's amnesia: `music stop` clears the
intent key, which used to be the only record that anything had played.
"""

import time

from agent_media_core.state import StateStore


A = "mpv:https://www.youtube.com/watch?v=aaaaaaaaaaa"
B = "mpv:https://www.youtube.com/watch?v=bbbbbbbbbbb"


def _store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "state.db")


def test_starting_music_records_it(tmp_path):
    st = _store(tmp_path)
    st.set_music_intent(A, "dj-set")
    rows = st.recent_history(sink="music", limit=5)
    assert len(rows) == 1
    assert rows[0]["uri"] == A and rows[0]["content_type"] == "dj-set"


def test_opening_a_book_records_it(tmp_path):
    st = _store(tmp_path)
    st.set_book_last(A)
    rows = st.recent_history(sink="book", limit=5)
    assert len(rows) == 1 and rows[0]["uri"] == A


def test_replaying_the_same_thing_does_not_stack_up(tmp_path):
    # The popup re-sets the intent on a re-play; a run of identical rows would
    # push everything else out of a 20-row listing.
    st = _store(tmp_path)
    for _ in range(4):
        st.set_music_intent(A, "music")
    assert len(st.recent_history(sink="music", limit=10)) == 1


def test_coming_back_to_something_is_a_new_row(tmp_path):
    st = _store(tmp_path)
    st.set_music_intent(A, "music")
    st.set_music_intent(B, "music")
    st.set_music_intent(A, "music")
    uris = [r["uri"] for r in st.recent_history(sink="music", limit=10)]
    assert uris == [A, B, A]


def test_the_channels_do_not_bleed_into_each_other(tmp_path):
    st = _store(tmp_path)
    st.set_music_intent(A, "music")
    st.set_book_last(B)
    assert [r["uri"] for r in st.recent_history(sink="music", limit=10)] == [A]
    assert [r["uri"] for r in st.recent_history(sink="book", limit=10)] == [B]
    # ...and unfiltered gives both, newest first.
    assert len(st.recent_history(limit=10)) == 2


def test_dedup_is_per_channel(tmp_path):
    # The same URI on both channels is two different listenings.
    st = _store(tmp_path)
    st.set_music_intent(A, "music")
    st.set_book_last(A)
    assert len(st.recent_history(limit=10)) == 2


def test_an_empty_uri_records_nothing(tmp_path):
    st = _store(tmp_path)
    st.note_play("music", "")
    st.note_play("music", "   ")
    assert st.recent_history(sink="music", limit=5) == []


def test_note_play_never_raises(tmp_path):
    # Playback must not fall over because a history write did. The row is a
    # convenience; the music is the point.
    st = _store(tmp_path)

    def boom(*a, **kw):
        raise RuntimeError("disk on fire")

    st.add_history = boom
    assert st.note_play("music", A) is None


# ---- the amnesia fix ------------------------------------------------------

def test_music_last_survives_a_stop(tmp_path):
    st = _store(tmp_path)
    st.set_music_intent(A, "dj-set")
    st.clear_music_intent()  # what music_stop does
    assert st.get_music_intent() is None
    last = st.get_music_last()
    assert last == {"uri": A, "content_type": "dj-set"}


def test_music_last_is_the_most_recent(tmp_path):
    st = _store(tmp_path)
    st.set_music_intent(A, "music")
    st.set_music_intent(B, "audiobook")
    assert st.get_music_last()["uri"] == B


def test_music_last_is_none_before_anything_plays(tmp_path):
    assert _store(tmp_path).get_music_last() is None


def test_music_last_ignores_the_other_channels(tmp_path):
    st = _store(tmp_path)
    st.set_book_last(B)
    st.add_history(sink="speech", uri="clip", started_at=time.time())
    assert st.get_music_last() is None
