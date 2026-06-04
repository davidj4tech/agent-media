"""Tests for book-channel playlists (phase 3).

A book playlist is an ordered list of part URIs with a remembered cursor.
Within-part offset resume reuses the per-URI resume_pos bookmarks; the
playlist only tracks which part (cur_index). These tests exercise the
state-store layer that holds that model — creation, ordered append, the
cursor, the active-playlist pointer, and deletion.
"""

import pytest

from agent_media_core.state import StateStore


A = "yt:https://youtu.be/part1"
B = "yt:https://youtu.be/part2"
C = "yt:https://youtu.be/part3"


def _store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "state.db")


def test_create_is_idempotent_and_reports(tmp_path):
    st = _store(tmp_path)
    assert st.create_playlist("dune") is True
    assert st.create_playlist("dune") is False  # already exists
    assert st.get_playlist("dune")["items"] == []
    assert st.get_playlist("dune")["cur_index"] == 0


def test_get_missing_playlist_is_none(tmp_path):
    assert _store(tmp_path).get_playlist("nope") is None


def test_add_items_preserves_order_and_appends(tmp_path):
    st = _store(tmp_path)
    st.create_playlist("dune")
    assert st.add_playlist_items("dune", [A, B]) == 2
    assert st.add_playlist_items("dune", [C]) == 3
    uris = [it["uri"] for it in st.get_playlist("dune")["items"]]
    assert uris == [A, B, C]
    positions = [it["pos"] for it in st.get_playlist("dune")["items"]]
    assert positions == [0, 1, 2]


def test_add_items_accepts_uri_title_pairs(tmp_path):
    st = _store(tmp_path)
    st.create_playlist("dune")
    st.add_playlist_items("dune", [(A, "Chapter 1"), B])
    items = st.get_playlist("dune")["items"]
    assert items[0]["title"] == "Chapter 1"
    assert items[1]["title"] is None


def test_add_to_missing_playlist_raises(tmp_path):
    with pytest.raises(KeyError):
        _store(tmp_path).add_playlist_items("ghost", [A])


def test_get_playlist_item_by_index(tmp_path):
    st = _store(tmp_path)
    st.create_playlist("dune")
    st.add_playlist_items("dune", [A, B])
    assert st.get_playlist_item("dune", 1)["uri"] == B
    assert st.get_playlist_item("dune", 5) is None


def test_cursor_moves_and_clamps(tmp_path):
    st = _store(tmp_path)
    st.create_playlist("dune")
    st.set_playlist_index("dune", 2)
    assert st.get_playlist("dune")["cur_index"] == 2
    st.set_playlist_index("dune", -3)
    assert st.get_playlist("dune")["cur_index"] == 0


def test_list_playlists_reports_counts(tmp_path):
    st = _store(tmp_path)
    st.create_playlist("dune")
    st.add_playlist_items("dune", [A, B])
    st.create_playlist("pod", channel="book")
    listing = {p["name"]: p for p in st.list_playlists(channel="book")}
    assert listing["dune"]["count"] == 2
    assert listing["pod"]["count"] == 0


def test_active_pointer_round_trips_and_clears(tmp_path):
    st = _store(tmp_path)
    assert st.get_playlist_active() is None
    st.set_playlist_active("dune")
    assert st.get_playlist_active() == "dune"
    st.clear_playlist_active()
    assert st.get_playlist_active() is None


def test_delete_removes_items_and_active(tmp_path):
    st = _store(tmp_path)
    st.create_playlist("dune")
    st.add_playlist_items("dune", [A, B])
    st.set_playlist_active("dune")
    assert st.delete_playlist("dune") is True
    assert st.get_playlist("dune") is None
    assert st.get_playlist_item("dune", 0) is None  # items gone too
    assert st.get_playlist_active() is None          # active cleared
    assert st.delete_playlist("dune") is False        # already gone


class _FakeBook:
    """Stand-in for SinkBook so auto-advance can be tested without mpv."""

    def __init__(self):
        self.plays = []

    def play(self, uri, target, start_ms=None):
        self.plays.append((uri, start_ms))

    def position(self, target):
        return None

    def idle(self, target):
        return True


def _wire_mcp(monkeypatch, tmp_path):
    import agent_media_core.mcp_server as m
    st = StateStore(tmp_path / "state.db")
    fb = _FakeBook()
    monkeypatch.setattr(m._state, "_v", st, raising=False)
    monkeypatch.setattr(m._book, "_v", fb, raising=False)
    # Don't spawn the real watcher thread (it'd dial a nonexistent socket).
    monkeypatch.setattr(m, "_ensure_autoadvance_watcher", lambda: None)
    return m, st, fb


def test_eof_advances_active_playlist(tmp_path, monkeypatch):
    m, st, fb = _wire_mcp(monkeypatch, tmp_path)
    st.create_playlist("dune")
    st.add_playlist_items("dune", [A, B, C])
    m.book_playlist_play("dune")
    assert st.get_playlist("dune")["cur_index"] == 0
    m._advance_after_eof()
    assert st.get_playlist("dune")["cur_index"] == 1
    m._advance_after_eof()
    assert st.get_playlist("dune")["cur_index"] == 2
    # URIs were normalized (yt: stripped) and played in order.
    assert [u for (u, _) in fb.plays] == [
        "https://youtu.be/part1", "https://youtu.be/part2",
        "https://youtu.be/part3"]


def test_eof_at_end_finishes_and_clears(tmp_path, monkeypatch):
    m, st, fb = _wire_mcp(monkeypatch, tmp_path)
    st.create_playlist("dune")
    st.add_playlist_items("dune", [A, B])
    m.book_playlist_play("dune")
    m._advance_after_eof()  # -> part B (last)
    assert st.get_playlist_active() == "dune"
    m._advance_after_eof()  # past the end
    assert st.get_playlist_active() is None
    assert st.get_now_playing("book") is None
    # No phantom extra play past the end.
    assert len(fb.plays) == 2


def test_eof_without_active_playlist_is_noop(tmp_path, monkeypatch):
    m, st, fb = _wire_mcp(monkeypatch, tmp_path)
    m._advance_after_eof()
    assert fb.plays == []


def test_per_part_resume_is_independent_of_cursor(tmp_path):
    # The two-level model: cur_index picks the part, resume_pos (by URI) the
    # offset within it. They are stored separately and don't interfere.
    st = _store(tmp_path)
    st.create_playlist("dune")
    st.add_playlist_items("dune", [A, B])
    st.set_resume_pos(B, 90_000)
    st.set_playlist_index("dune", 1)
    assert st.get_playlist("dune")["cur_index"] == 1
    cur = st.get_playlist_item("dune", st.get_playlist("dune")["cur_index"])
    assert cur["uri"] == B
    assert st.get_resume_pos(cur["uri"]) == 90_000
