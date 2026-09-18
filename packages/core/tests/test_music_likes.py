"""`media music like` — keeping what was playing, whichever sink it was on.

The ask this came from (relayed 2026-09-18): `media music bookmark` answered
"no music loaded" about a track playing on the phone, `media music status
--json` showed uri and title null while `media music now` named the track and
its chapter, and there was nowhere to record "this one, this chapter, keep it".
"""

from __future__ import annotations

import pytest


PHONE_PROPS = {
    "idle-active": False, "pause": False, "time-pos": 3840.0,
    "duration": 7200.0, "speed": 1.0, "volume": 130,
    "media-title": "ogn-z3GJzeA.m4a",
    "chapter-metadata/by-key/title": "Happy & Free (Lokah) — Steve Gold",
    "path": "/data/data/com.termux/files/home/.cache/music-offline/ogn-z3GJzeA.m4a",
}


# ---- what names the thing ---------------------------------------------------

@pytest.mark.parametrize("live,want", [
    ("yt:https://www.youtube.com/watch?v=ogn-z3GJzeA", "ogn-z3GJzeA"),
    ("https://youtu.be/ogn-z3GJzeA", "ogn-z3GJzeA"),
    # The phone plays its own cached copy; the id is the filename.
    ("/home/ryer/.cache/music-offline/ogn-z3GJzeA.m4a", "ogn-z3GJzeA"),
    ("/music/Steve Gold/track.flac", "/music/Steve Gold/track.flac"),
    ("", ""),
])
def test_the_id_is_the_video_id_wherever_it_is_visible(live, want):
    from agent_media_core import cli
    assert cli._music_media_id(live) == want


@pytest.mark.parametrize("where,want", [
    ("phone", "phone"), ("rooms", "rooms"), ("local", "rooms"),
    # Not named by the caller: "" and "default" must NOT pin the read, or a
    # host whose default target is the phone can never read Mopidy.
    ("", ""), ("default", ""), ("auto", ""),
])
def test_only_a_named_backend_pins_the_read(where, want):
    from agent_media_core import cli
    assert cli._music_named_backend(where) == want


def test_the_uri_kept_is_the_one_that_was_asked_for(monkeypatch):
    """A cache path names a file on a phone; `yt:…` names a mix anyone can
    put on again. The title comes from the history row even though the URI
    came from the intent row."""
    from agent_media_core import cli

    class FakeStore:
        def get_music_intent(self):
            return {"uri": "yt:https://www.youtube.com/watch?v=ogn-z3GJzeA"}

        def recent_history(self, sink=None, limit=20):
            return [{"uri": "https://youtu.be/ogn-z3GJzeA",
                     "text": "vaniBorn · Spiritual Morning Mix"}]

    monkeypatch.setattr(cli, "StateStore", lambda: FakeStore())
    uri, name = cli._music_asked("ogn-z3GJzeA", "/cache/ogn-z3GJzeA.m4a")
    assert uri == "yt:https://www.youtube.com/watch?v=ogn-z3GJzeA"
    assert name == "vaniBorn · Spiritual Morning Mix"


def test_nothing_on_record_keeps_what_is_live(monkeypatch):
    from agent_media_core import cli

    class FakeStore:
        def get_music_intent(self): return None
        def recent_history(self, sink=None, limit=20): return []

    monkeypatch.setattr(cli, "StateStore", lambda: FakeStore())
    assert cli._music_asked("ogn-z3GJzeA", "/cache/ogn-z3GJzeA.m4a") == \
        ("/cache/ogn-z3GJzeA.m4a", "")


# ---- the snapshot -----------------------------------------------------------

def test_the_phone_snapshot_carries_the_chapter_and_the_real_name(monkeypatch):
    from agent_media_core import cli

    monkeypatch.setattr(cli, "_phone_music_props", lambda patient=False: PHONE_PROPS)
    monkeypatch.setattr(cli, "_music_asked", lambda media_id, live: (
        "yt:https://www.youtube.com/watch?v=ogn-z3GJzeA", "Spiritual Morning Mix"))
    monkeypatch.setattr(cli, "_music_hold_active", lambda: False)
    snap = cli._music_status_json(object(), patient=True)
    assert snap["backend"] == "phone"
    assert snap["media_id"] == "ogn-z3GJzeA"
    assert snap["uri"] == "yt:https://www.youtube.com/watch?v=ogn-z3GJzeA"
    assert snap["chapter"] == "Happy & Free (Lokah) — Steve Gold"
    # An unembedded download reports `<id>.<ext>`, which trims to the bare id
    # — the one thing nobody can read. The cache's name replaces it.
    assert snap["title"].endswith("Spiritual Morning Mix")
    assert snap["pos_ms"] == 3840000 and snap["dur_ms"] == 7200000
    assert snap["path"].endswith("ogn-z3GJzeA.m4a")


def test_a_phone_read_that_fails_does_not_answer_about_mopidy(monkeypatch):
    """`--where phone` asked about the phone. Reporting an idle Mopidy queue
    instead is how a phone-targeted read came back all nulls."""
    from agent_media_core import cli

    monkeypatch.setattr(cli, "_phone_music_props", lambda patient=False: None)
    monkeypatch.setattr(cli, "_music_hold_active", lambda: False)

    def boom(*a, **k):
        raise AssertionError("asked Mopidy about a phone-targeted read")

    snap = cli._music_status_json(type("M", (), {"status_dict": boom})(),
                                  where="phone")
    assert snap["backend"] == "phone" and snap["uri"] is None


def test_a_rooms_read_never_probes_the_phone(monkeypatch):
    from agent_media_core import cli

    def boom(patient=False):
        raise AssertionError("probed the phone for a rooms-targeted read")

    monkeypatch.setattr(cli, "_phone_music_props", boom)
    monkeypatch.setattr(cli, "_music_hold_active", lambda: False)
    snap = cli._music_status_json(
        type("M", (), {"status_dict": lambda self: {"state": "stop"}})(),
        where="rooms")
    assert snap["backend"] == "mopidy"


def test_an_mpv_routed_rooms_track_gets_the_renderers_numbers(monkeypatch):
    """MPD reports no duration and filename-only tags for an mpv-routed
    track; the popup has always read the renderer, and now so does the
    structured form."""
    from agent_media_core import cli
    from agent_media_core.sinks import music as music_mod

    class FakeMopidy:
        def status_dict(self): return {"state": "play", "volume": "80"}
        def now_playing_uri(self): return "mpv:/srv/music/mix.m4a"

    monkeypatch.setattr(cli, "_phone_music_props", lambda patient=False: None)
    monkeypatch.setattr(cli, "_music_hold_active", lambda: False)
    monkeypatch.setattr(cli, "_music_now_label", lambda m: "mix.m4a")
    monkeypatch.setattr(music_mod, "mpv_now_props", lambda: {
        "time-pos": 90.0, "duration": 3600.0, "pause": False, "speed": 1.0,
        "media-title": "A Long Mix",
        "chapter-metadata/by-key/title": "Track 3"})
    snap = cli._music_status_json(FakeMopidy())
    assert snap["renderer"] == "mpv"
    assert snap["title"] == "Track 3 · A Long Mix"
    assert snap["pos_ms"] == 90000 and snap["dur_ms"] == 3600000


# ---- the like itself --------------------------------------------------------

def test_a_like_is_recorded_with_the_chapter_it_was_in(monkeypatch, capsys, tmp_path):
    from agent_media_core import cli
    from agent_media_core.state import StateStore

    st = StateStore(tmp_path / "state.db")
    monkeypatch.setattr(cli, "StateStore", lambda: st)
    monkeypatch.setattr(cli, "_music_snapshot", lambda m, where="": {
        "backend": "phone", "media_id": "ogn-z3GJzeA",
        "uri": "yt:https://www.youtube.com/watch?v=ogn-z3GJzeA",
        "title": "Happy & Free (Lokah) · Spiritual Morning Mix",
        "chapter": "Happy & Free (Lokah)", "pos_ms": 3840000,
        "dur_ms": 7200000, "path": "/cache/ogn-z3GJzeA.m4a"})
    assert cli._music_like(object(), "on the deck") == 0
    out = capsys.readouterr().out
    assert "♥" in out and "on the deck" in out

    rows = st.list_likes(channel="music")
    assert len(rows) == 1
    like = rows[0]
    assert like["media_id"] == "ogn-z3GJzeA"
    assert like["chapter"] == "Happy & Free (Lokah)"
    assert like["pos_ms"] == 3840000
    assert like["backend"] == "phone"
    assert like["note"] == "on the deck"
    assert like["extras"] == {"path": "/cache/ogn-z3GJzeA.m4a"}


def test_liking_the_same_mix_twice_keeps_both(monkeypatch, tmp_path):
    """Not a bookmark: two chapters of one mix are two facts, and the second
    must not overwrite the first."""
    from agent_media_core import cli
    from agent_media_core.state import StateStore

    st = StateStore(tmp_path / "state.db")
    monkeypatch.setattr(cli, "StateStore", lambda: st)
    for chap, pos in (("Lokah", 3840000), ("Gayatri", 5400000)):
        monkeypatch.setattr(cli, "_music_snapshot", lambda m, where="", c=chap, p=pos: {
            "backend": "phone", "media_id": "ogn-z3GJzeA", "uri": "yt:x",
            "title": "Spiritual Morning Mix", "chapter": c, "pos_ms": p,
            "dur_ms": 7200000, "path": ""})
        assert cli._music_like(object()) == 0
    rows = st.list_likes(channel="music")
    assert [r["chapter"] for r in rows] == ["Gayatri", "Lokah"]     # newest first


def test_liking_nothing_says_so(monkeypatch, capsys):
    from agent_media_core import cli

    monkeypatch.setattr(cli, "_music_snapshot", lambda m, where="": None)
    assert cli._music_like(object(), "") == 1
    assert "no music loaded" in capsys.readouterr().err


def test_likes_lists_newest_first_and_as_json(monkeypatch, capsys, tmp_path):
    from agent_media_core import cli
    from agent_media_core.state import StateStore
    import json

    st = StateStore(tmp_path / "state.db")
    st.add_like("music", "aaaaaaaaaaa", title="First", pos_ms=1000, at=100.0)
    st.add_like("music", "bbbbbbbbbbb", title="Second", pos_ms=2000,
                note="keep", at=200.0)
    monkeypatch.setattr(cli, "StateStore", lambda: st)
    assert cli._cmd_likes("", channel="music") == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert "Second" in lines[0] and "keep" in lines[0]
    assert "First" in lines[1]

    assert cli._cmd_likes("", channel="music", json_out=True) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["title"] for r in rows] == ["Second", "First"]


def test_a_backfilled_like_keeps_the_time_it_was_given(tmp_path):
    from agent_media_core.state import StateStore

    st = StateStore(tmp_path / "state.db")
    st.add_like("music", "ogn-z3GJzeA", title="Spiritual Morning Mix",
                at=1_700_000_000.0)
    assert st.list_likes()[0]["at"] == 1_700_000_000.0
