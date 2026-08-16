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


# ---- titles that only turn up after the file loads ------------------------
#
# A row is written when something is *put on*, when a URI is all anyone has.
# mpv learns the real name a moment later. Without a way back, the listing is
# filenames — `td565-video-2026-08-11-15-42-38.mp3` for an episode with a
# perfectly good title.

EPISODE = ("https://content.libsyn.com/p/4/8/7/td565-video-2026-08-11.mp3"
           "?Expires=1786861383&Signature=abc")


def test_a_title_can_arrive_late(tmp_path):
    st = _store(tmp_path)
    st.set_book_last(EPISODE)
    assert st.set_history_title("book", EPISODE, "TD 565 — Drupal in 2026")
    assert st.recent_history(sink="book")[0]["text"] == "TD 565 — Drupal in 2026"


def test_a_filename_title_is_replaced(tmp_path):
    # book_play derives one from the URL when it has nothing better; that
    # derived name is exactly what this is here to improve on.
    st = _store(tmp_path)
    st.set_book_last(EPISODE, "td565-video-2026-08-11")
    assert st.set_history_title("book", EPISODE, "TD 565 — Drupal in 2026")
    assert st.recent_history(sink="book")[0]["text"] == "TD 565 — Drupal in 2026"


def test_a_real_title_is_never_overwritten(tmp_path):
    # A share knows the title before it plays. mpv's media-title for the same
    # file may be worse (an embedded tag, a CDN filename); first good one wins.
    st = _store(tmp_path)
    st.set_music_intent(A, "music", "Me at the zoo")
    assert not st.set_history_title("music", A, "aaaaaaaaaaa.mka")
    assert st.recent_history(sink="music")[0]["text"] == "Me at the zoo"


def test_only_the_newest_listen_is_named(tmp_path):
    # Playing A, then B, then A again gives two A rows; the title belongs to
    # the one just started, not to last week's.
    st = _store(tmp_path)
    st.set_music_intent(A, "music")
    st.set_music_intent(B, "music")
    st.set_music_intent(A, "music")
    st.set_history_title("music", A, "A Long Set")
    rows = st.recent_history(sink="music", limit=10)
    assert rows[0]["text"] == "A Long Set"     # the newest A
    assert not rows[2]["text"]                 # the older A, untouched


def test_a_title_for_something_never_played_is_ignored(tmp_path):
    st = _store(tmp_path)
    assert not st.set_history_title("music", "never-played", "Whatever")
    assert st.recent_history(limit=5) == []


def test_empty_titles_and_uris_do_nothing(tmp_path):
    st = _store(tmp_path)
    st.set_music_intent(A, "music")
    assert not st.set_history_title("music", A, "")
    assert not st.set_history_title("music", A, "   ")
    assert not st.set_history_title("music", "", "A Title")


def test_the_same_title_twice_is_not_a_write(tmp_path):
    st = _store(tmp_path)
    st.set_book_last(EPISODE)
    assert st.set_history_title("book", EPISODE, "TD 565")
    assert not st.set_history_title("book", EPISODE, "TD 565")


def test_the_channels_do_not_cross(tmp_path):
    st = _store(tmp_path)
    st.set_music_intent(A, "music")
    st.set_book_last(A)
    st.set_history_title("book", A, "The Book One")
    assert st.recent_history(sink="book")[0]["text"] == "The Book One"
    assert not st.recent_history(sink="music")[0]["text"]
