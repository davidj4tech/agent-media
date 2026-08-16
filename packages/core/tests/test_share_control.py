"""The popup's transport, as data.

`share_control` is what a non-terminal surface renders and presses. Two
properties matter more than any single field:

  - a snapshot never raises, because a control surface that cannot read one
    channel must still draw the other two;
  - a verb is a whitelist entry, because the listener's job is transport and
    not remote execution of a large CLI.
"""

import pytest

from agent_media_core.entrypoints import share_control as sc


# ---- the whitelist --------------------------------------------------------

def test_a_verb_becomes_the_command_media_already_understands():
    seen = []
    sc.control("music", "toggle", runner=lambda argv: seen.append(argv) or 0)
    assert seen == [["music", "toggle"]]


def test_an_argument_lands_where_the_placeholder_is():
    seen = []
    sc.control("music", "seek", "+30", runner=lambda argv: seen.append(argv) or 0)
    assert seen == [["music", "seek", "+30"]]


def test_prev_restarts_first_like_the_popup_does():
    # The popup's `<` is ⏮ semantics: restart the track if past its start.
    seen = []
    sc.control("book", "prev", runner=lambda argv: seen.append(argv) or 0)
    assert seen == [["book", "prev", "--restart-first"]]


def test_speech_verbs_are_the_bare_commands():
    seen = []
    sc.control("speech", "toggle", runner=lambda argv: seen.append(argv) or 0)
    sc.control("speech", "mute", runner=lambda argv: seen.append(argv) or 0)
    assert seen == [["toggle"], ["mute"]]


def test_a_channel_publishes_the_verbs_it_takes():
    # So a front end never draws a button that can only be refused. The phone
    # drew `mute` on the book channel until this existed.
    assert "mute" in sc.verbs("speech")
    assert "mute" not in sc.verbs("book")
    assert "volume" not in sc.verbs("book")
    assert "chapter" not in sc.verbs("book")
    assert {"toggle", "seek", "speed", "next", "prev"} <= set(sc.verbs("book"))
    # And every published verb is one control() will actually accept.
    for channel in sc.CHANNELS:
        for action in sc.verbs(channel):
            assert (channel, action) in sc.VERBS


def test_the_snapshot_carries_them():
    blank = sc._blank("book")
    assert blank["verbs"] == sc.verbs("book")


def test_an_unknown_verb_is_refused():
    with pytest.raises(sc.ControlError):
        sc.control("music", "rm -rf", runner=lambda argv: 0)
    with pytest.raises(sc.ControlError):
        sc.control("music", "play", runner=lambda argv: 0)   # not transport
    with pytest.raises(sc.ControlError):
        sc.control("speech", "chapter", runner=lambda argv: 0)  # wrong channel


def test_a_verb_that_needs_a_value_says_so():
    with pytest.raises(sc.ControlError):
        sc.control("music", "seek", "", runner=lambda argv: 0)


def test_an_argument_is_never_split_into_more_arguments():
    # It reaches `media` as one argv entry, so there is no shell to confuse.
    seen = []
    sc.control("music", "seek", "; rm -rf /",
               runner=lambda argv: seen.append(argv) or 0)
    assert seen == [["music", "seek", "; rm -rf /"]]


def test_an_extra_argument_to_a_verb_that_takes_none_is_ignored():
    seen = []
    sc.control("music", "toggle", "99", runner=lambda argv: seen.append(argv) or 0)
    assert seen == [["music", "toggle"]]


def test_control_returns_the_exit_code():
    assert sc.control("music", "toggle", runner=lambda argv: 3) == 3


def test_every_whitelisted_verb_names_a_real_channel():
    for channel, action in sc.VERBS:
        assert channel in sc.CHANNELS, (channel, action)


# ---- snapshots ------------------------------------------------------------

def test_a_snapshot_has_all_three_channels(monkeypatch):
    # With nothing running — which is the state a dev box is usually in — every
    # channel still answers, idle.
    snap = sc.channels()
    assert set(snap) >= {"speech", "music", "book"}
    for name in sc.CHANNELS:
        assert snap[name]["channel"] == name


def test_a_broken_backend_is_an_idle_channel_not_an_exception(monkeypatch):
    def boom(*a, **kw):
        raise OSError("mpv is gone")

    monkeypatch.setattr("agent_media_core.cli._speech_display_state", boom)
    monkeypatch.setattr("agent_media_core.cli._music_status_json", boom)
    monkeypatch.setattr("agent_media_core.mcp_server.book_now_playing", boom)

    snap = sc.channels()
    for name in sc.CHANNELS:
        assert snap[name]["idle"] is True
        assert snap[name]["playing"] is False
        assert snap[name]["pos_ms"] is None


def test_music_is_playing_only_when_something_is_loaded_and_unpaused(monkeypatch):
    def snap(_router):
        return {"backend": "phone", "uri": "mpv:x", "title": "A Set",
                "chapter": "ch 2", "pos_ms": 1000, "dur_ms": 9000,
                "paused": False, "speed": 1.25, "volume": 130, "held": False}

    monkeypatch.setattr("agent_media_core.cli._music_status_json", snap)
    music = sc.channels()["music"]
    assert music["playing"] and not music["idle"]
    assert music["title"] == "A Set" and music["chapter"] == "ch 2"
    assert music["speed"] == 1.25 and music["volume"] == 130


def test_a_paused_track_is_loaded_but_not_playing(monkeypatch):
    monkeypatch.setattr(
        "agent_media_core.cli._music_status_json",
        lambda _r: {"backend": "phone", "uri": "mpv:x", "paused": True})
    music = sc.channels()["music"]
    assert not music["playing"] and not music["idle"] and music["paused"]


def test_an_idle_book_reports_nothing_to_draw(monkeypatch):
    monkeypatch.setattr("agent_media_core.mcp_server.book_now_playing",
                        lambda target="": {"idle": True})
    assert sc.channels()["book"]["idle"] is True


def test_a_book_snapshot_carries_what_the_screen_shows(monkeypatch):
    monkeypatch.setattr(
        "agent_media_core.mcp_server.book_now_playing",
        lambda target="": {"idle": False, "uri": "u", "media_title": "TD 565",
                           "chapter_title": "Part 2", "position_ms": 12000,
                           "duration_ms": 3600000, "paused": False,
                           "speed": 1.5})
    book = sc.channels()["book"]
    assert book["title"] == "TD 565" and book["chapter"] == "Part 2"
    assert book["pos_ms"] == 12000 and book["dur_ms"] == 3600000
    assert book["playing"] and book["speed"] == 1.5


# ---- chapters -------------------------------------------------------------

def test_chapters_are_numbered_from_one_with_the_current_marked(monkeypatch):
    monkeypatch.setattr(
        "agent_media_core.cli._music_mpv_chapters",
        lambda: ("ep", [{"title": "Intro", "time": 0.0},
                        {"title": "Second", "time": 252.0},
                        {"title": "", "time": 900.0}], 1))
    rows = sc.chapters()
    assert [r["number"] for r in rows] == [1, 2, 3]
    assert rows[1]["current"] and not rows[0]["current"]
    assert rows[1]["start_ms"] == 252000
    # An untitled chapter still needs something to tap.
    assert rows[2]["title"] == "Chapter 3"


def test_no_live_track_is_an_empty_list_not_an_error(monkeypatch):
    monkeypatch.setattr("agent_media_core.cli._music_mpv_chapters", lambda: None)
    assert sc.chapters() == []


def test_a_chapter_read_that_explodes_is_still_an_empty_list(monkeypatch):
    def boom():
        raise OSError("bridge down")

    monkeypatch.setattr("agent_media_core.cli._music_mpv_chapters", boom)
    assert sc.chapters() == []
