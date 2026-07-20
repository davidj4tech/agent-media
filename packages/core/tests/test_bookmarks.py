"""Media bookmarks: the generic (channel, media_id) bookmark store + the
CLI capture path (point bookmarks, ranges, named registers).
"""

from __future__ import annotations


# ---- store roundtrip ---------------------------------------------------------

def test_bookmark_store_roundtrip(tmp_path):
    from agent_media_core.state import StateStore

    st = StateStore(tmp_path / "state.db")
    st.set_bookmark("music", "abc123def45", "https://youtu.be/abc123def45",
                    90000, title="Mix", duration_ms=600000,
                    note="good bit", transcript="searchable words",
                    extras={"backend": "phone"})
    bm = st.get_bookmark("music", "abc123def45")
    assert bm["pos_ms"] == 90000
    assert bm["title"] == "Mix"
    assert bm["note"] == "good bit"
    assert bm["transcript"] == "searchable words"
    assert bm["extras"] == {"backend": "phone"}
    assert st.list_bookmarks(channel="music")[0]["media_id"] == "abc123def45"


# ---- CLI capture path --------------------------------------------------------

def test_music_bookmark_command_saves_live_position(monkeypatch):
    from agent_media_core import cli

    class FakeBackend:
        def now_playing_uri(self): return "https://youtu.be/a82hE1aupo8"
        def position(self): return 123000

    saved = {}

    class FakeStore:
        def get_bookmark_pending(self, channel, slot=""): return None
        def set_bookmark(self, **kwargs): saved.update(kwargs)
        def set_bookmark_pending(self, channel, data, slot=""): pass

    monkeypatch.setattr(cli, "_music_live_backend", lambda m: FakeBackend())
    monkeypatch.setattr(cli, "_music_now_status",
                        lambda m, width, hide_idle, bar: ("", "Cool Mix", ""))
    monkeypatch.setattr(cli, "_phone_music_props", lambda: {"duration": 600})
    monkeypatch.setattr(cli, "StateStore", lambda: FakeStore())
    assert cli._music_bookmark(object(), "note") == 0
    assert saved["channel"] == "music"
    assert saved["media_id"] == "a82hE1aupo8@123000"
    assert saved["extras"]["item_id"] == "a82hE1aupo8"
    assert saved["pos_ms"] == 123000
    assert saved["title"] == "Cool Mix"
    assert saved["note"] == "note"
