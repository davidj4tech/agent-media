"""The popup's transport, as data.

`share_control` is what a non-terminal surface renders and presses. Two
properties matter more than any single field:

  - a snapshot never raises, because a control surface that cannot read one
    channel must still draw the other two;
  - a verb is a whitelist entry, because the listener's job is transport and
    not remote execution of a large CLI.
"""

import json

import pytest

from agent_media_core.entrypoints import share_control as sc


@pytest.fixture(autouse=True)
def _this_host_is_the_origin(monkeypatch):
    """No test may reach for ssh.

    Speech history is asked of the origin, and "unset" means read the config
    file — which on a developer's own machine names a real hub. Declaring this
    host the origin is the standalone answer: nobody to ask. Tests about the
    hop say so by patching `_origin_host` themselves.
    """
    monkeypatch.setenv("MEDIA_ROLES", "origin render")


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


def test_speech_prev_and_next_step_the_reader():
    # Named for the button, not the thing: a front end's ⏮/⏭ is a sentence
    # here, and the direction rides in the argv because a transport button has
    # no argument to send.
    seen = []
    sc.control("speech", "prev", runner=lambda argv: seen.append(argv) or 0)
    sc.control("speech", "next", runner=lambda argv: seen.append(argv) or 0)
    assert seen == [["skip", "--dir", "-1"], ["skip", "--dir", "1"]]


def test_a_channel_publishes_the_verbs_it_takes():
    # So a front end never draws a button that can only be refused. The phone
    # drew `mute` on the book channel until this existed.
    assert "mute" in sc.verbs("speech")
    assert "mute" not in sc.verbs("book")
    assert "volume" not in sc.verbs("book")
    # `chapter` the book does take, since 2026-08-17: an m4b has chapters by
    # definition and mpv lifts a YouTube upload's marks.
    assert "chapter" in sc.verbs("book")
    # Speech takes it too, since 2026-08-17 — its "chapters" are its clips.
    assert "chapter" in sc.verbs("speech")
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


def test_the_book_reads_its_own_mpv(monkeypatch):
    # Not the music one — that was the whole bug: the book channel could not
    # show the chapters of the thing most likely to have them.
    monkeypatch.setattr(
        "agent_media_core.cli._music_mpv_chapters",
        lambda: ("music-ep", [{"title": "Wrong", "time": 0.0}], 0))
    monkeypatch.setattr(
        "agent_media_core.cli._book_mpv_chapters",
        lambda: ("book-ep", [{"title": "One", "time": 0.0},
                             {"title": "Two", "time": 600.0}], 1))
    rows = sc.chapters("book")
    assert [r["title"] for r in rows] == ["One", "Two"]
    assert rows[1]["current"] and rows[1]["start_ms"] == 600000


def test_an_unknown_channel_is_an_empty_list(monkeypatch):
    monkeypatch.setattr(
        "agent_media_core.cli._music_mpv_chapters",
        lambda: ("ep", [{"title": "Intro", "time": 0.0}], 0))
    assert sc.chapters("radio") == []


# ---- speech's chapters are its clips ---------------------------------------

def _clip(rid, at, text):
    return {"id": rid, "started_at": at, "text": text}


def test_speech_lists_the_clips_it_has_said(monkeypatch):
    # The button's question — what is in this, take me to that part — has an
    # answer on speech, and it is the history.
    monkeypatch.setattr("agent_media_core.cli._speech_history",
                        lambda n=20, **kw: [_clip(91, 1_700_000_000, "the last one"),
                                            _clip(90, 1_699_999_000, "one\nbefore")])
    monkeypatch.setattr("agent_media_core.cli._now_speaking", lambda: None)
    rows = sc.chapters("speech")
    assert [r["number"] for r in rows] == [1, 2]
    # Addressed by history id, not by position: the list is newest-first, so a
    # clip landing while the picker is open would renumber every row.
    assert [r["ref"] for r in rows] == ["91", "90"]
    assert rows[0]["title"].endswith("the last one")
    assert "  " in rows[0]["title"]          # a clock time, then the words
    assert rows[1]["title"].endswith("one before")   # one line, not two
    assert rows[0]["start_ms"] is None       # these are not one track
    assert not any(r["current"] for r in rows)


def test_the_replayed_clip_is_the_marked_one(monkeypatch):
    monkeypatch.setattr("agent_media_core.cli._speech_history",
                        lambda n=20, **kw: [_clip(91, 1_700_000_000, "newest"),
                                            _clip(90, 1_699_999_000, "older")])
    monkeypatch.setattr("agent_media_core.cli._now_speaking",
                        lambda: {"extras": {"history_id": 90}})
    rows = sc.chapters("speech")
    assert rows[1]["current"] and not rows[0]["current"]


def test_a_clip_with_no_record_is_not_offered(monkeypatch):
    # The live turn: history is written when a turn ends, so what is being
    # spoken has no id yet and `replay --id` could not find it.
    monkeypatch.setattr(
        "agent_media_core.cli._speech_history",
        lambda n=20, **kw: [_clip(None, 1_700_000_100, "speaking now"),
                            _clip(91, 1_700_000_000, "said")])
    monkeypatch.setattr("agent_media_core.cli._now_speaking", lambda: None)
    rows = sc.chapters("speech")
    assert [r["ref"] for r in rows] == ["91"]
    assert rows[0]["number"] == 1


def test_picking_a_clip_replays_that_record():
    seen = []
    sc.control("speech", "chapter", "91", runner=lambda argv: seen.append(argv) or 0)
    assert seen == [["replay", "--id", "91"]]


def test_a_render_host_asks_the_origin_for_the_words(monkeypatch):
    # The phone records only what it rendered itself, which since July is
    # nothing: its own store captioned today's audio with a July sentence.
    monkeypatch.setattr(sc, "_clips_cache", {"at": 0.0, "rows": None})
    monkeypatch.setattr(sc, "_origin_host", lambda: "red5")
    asked = []
    monkeypatch.setattr(sc, "_ask_origin", lambda argv, **kw: asked.append(argv)
                        or json.dumps([{"number": 1, "title": "18:29  today",
                                        "text": "today", "start_ms": None,
                                        "current": False, "ref": "5506"}]))
    rows = sc.chapters("speech")
    assert [r["ref"] for r in rows] == ["5506"]
    assert asked == [["history", "40", "--json"]]


def test_the_hub_being_away_leaves_the_phone_its_own_clips(monkeypatch):
    # Short and old, but true — and a picker with something in it beats one
    # that says the machine is down.
    monkeypatch.setattr(sc, "_clips_cache", {"at": 0.0, "rows": None})
    monkeypatch.setattr(sc, "_origin_host", lambda: "red5")
    monkeypatch.setattr(sc, "_ask_origin", lambda argv, **kw: None)
    monkeypatch.setattr("agent_media_core.cli._speech_history",
                        lambda n=20, **kw: [_clip(7, 1_699_000_000, "in July")])
    monkeypatch.setattr("agent_media_core.cli._now_speaking", lambda: None)
    assert [r["ref"] for r in sc.chapters("speech")] == ["7"]


def test_the_title_reads_the_same_list_but_not_every_second(monkeypatch):
    # The card polls about once a second; the ask is cached, and a tap on the
    # picker is what forces a fresh one.
    monkeypatch.setattr(sc, "_clips_cache", {"at": 0.0, "rows": None})
    monkeypatch.setattr(sc, "_origin_host", lambda: "red5")
    asks = []
    monkeypatch.setattr(sc, "_ask_origin", lambda argv, **kw: asks.append(argv)
                        or json.dumps([{"number": 1, "title": "18:29  today",
                                        "text": "today", "ref": "5506",
                                        "start_ms": None, "current": False}]))
    assert sc._clips()[0]["text"] == "today"
    sc._clips()
    sc._clips()
    assert len(asks) == 1
    sc.chapters("speech")
    assert len(asks) == 2


def test_a_failed_ask_is_not_re_dialled_every_poll(monkeypatch):
    # Eight seconds of timeout apiece, once a second, for as long as the app
    # is open — a hub that is asleep must be asked at the same rate as one
    # that answered.
    monkeypatch.setattr(sc, "_clips_cache", {"at": 0.0, "rows": None})
    monkeypatch.setattr(sc, "_origin_host", lambda: "red5")
    asks = []
    monkeypatch.setattr(sc, "_ask_origin", lambda argv, **kw: asks.append(argv))
    monkeypatch.setattr("agent_media_core.cli._speech_history", lambda n=20, **kw: [])
    monkeypatch.setattr("agent_media_core.cli._now_speaking", lambda: None)
    sc._clips()
    sc._clips()
    sc._clips()
    assert len(asks) == 1


def test_replaying_from_a_render_host_runs_where_the_clips_are(monkeypatch):
    # Both verbs name a past turn, and the phone knows none of them. Run on the
    # origin they come back out of this host's speakers anyway — it is the
    # speech target, so this is the ordinary push path.
    monkeypatch.setattr(sc, "_origin_host", lambda: "red5")
    ran = []
    monkeypatch.setattr(sc, "_ask_origin", lambda argv, **kw: ran.append(argv) or "")
    assert sc.control("speech", "chapter", "5506") == 0
    assert sc.control("speech", "replay", "1") == 0
    assert ran == [["replay", "--id", "5506"], ["replay", "1"]]


def test_play_after_a_clip_has_finished_is_a_history_press(monkeypatch):
    # `media toggle` means pause with a reply in flight and "replay the last
    # turn" with nothing loaded — and the second one was reaching into the only
    # turns the phone ever rendered itself, so play-after-the-end played July.
    monkeypatch.setattr(sc, "_origin_host", lambda: "red5")
    ran = []
    monkeypatch.setattr(sc, "_ask_origin", lambda argv, **kw: ran.append(argv) or "")

    # Parked at the end: sink-speech leaves the finished clip loaded and
    # un-paused, so "idle" is False and only position-meets-duration says so.
    monkeypatch.setattr(sc, "_speech", lambda: {"idle": False, "pos_ms": 90360,
                                                "dur_ms": 90360})
    assert sc.control("speech", "toggle") == 0
    assert ran == [["toggle"]]


def test_pause_mid_reply_stays_on_this_host(monkeypatch):
    # It must work with the hub asleep, and it must not cost a second.
    monkeypatch.setattr(sc, "_origin_host", lambda: "red5")
    monkeypatch.setattr(sc, "_ask_origin",
                        lambda argv, **kw: pytest.fail("pause went over ssh"))
    monkeypatch.setattr(sc, "_speech", lambda: {"idle": False, "pos_ms": 12000,
                                                "dur_ms": 90360})
    seen = []
    sc.control("speech", "toggle", runner=lambda argv: seen.append(argv) or 0)
    assert seen == [["toggle"]]
    assert not sc._nothing_to_resume()


def test_the_card_says_which_conversation_said_it(monkeypatch):
    # The words answer "what is this" and leave "who was that to" open. The
    # shade's card answers it; the app's had nothing to answer it with.
    monkeypatch.setattr(
        "agent_media_core.cli._speech_display_state",
        lambda **kw: (True, None, None, False, False, None, False))
    monkeypatch.setattr("agent_media_core.sinks._mpv_ipc.get_properties",
                        lambda sock, names, **kw: {})
    monkeypatch.setattr(sc, "_clips", lambda *a, **kw: [
        {"text": "a reply", "window": "add C function", "ref": "1"}])
    got = sc._speech()
    assert got["title"] == "a reply"
    assert got["conversation"] == "add C function"


def test_a_clip_with_no_conversation_says_none(monkeypatch):
    # Cron speaks from a pane that belongs to no conversation, and "" is not a
    # name — the card should draw nothing rather than an empty separator.
    monkeypatch.setattr(
        "agent_media_core.cli._speech_display_state",
        lambda **kw: (True, None, None, False, False, None, False))
    monkeypatch.setattr("agent_media_core.sinks._mpv_ipc.get_properties",
                        lambda sock, names, **kw: {})
    monkeypatch.setattr(sc, "_clips", lambda *a, **kw: [
        {"text": "moon enters Libra", "window": "", "ref": "1"}])
    assert sc._speech()["conversation"] is None


def test_every_channel_has_the_field(monkeypatch):
    # Nullable everywhere, like every other field here: a front end reads one
    # shape whichever channel it is looking at.
    assert sc._blank("music")["conversation"] is None


def _player(monkeypatch, **props):
    """The speech broker answering one batched read."""
    monkeypatch.setattr(
        "agent_media_core.cli._speech_display_state",
        lambda **kw: (False, 12.0, 90.0, False, False, 1.0, True))
    monkeypatch.setattr("agent_media_core.cli._sock", lambda: "/tmp/none.sock")
    monkeypatch.setattr("agent_media_core.sinks._mpv_ipc.get_properties",
                        lambda sock, names, **kw: dict(props))


def test_the_speech_card_knows_its_own_volume(monkeypatch):
    # The channel published a `volume` verb and the card had a place to show
    # one; the field was left None, so both buttons worked and said nothing —
    # which reads exactly like two buttons that do not work.
    _player(monkeypatch, volume=150.0)
    assert sc._speech()["volume"] == 150


def test_the_card_reads_the_player_before_the_history(monkeypatch):
    # What makes it keep up: the clip list is the origin's, cached for twenty
    # seconds and fetched over ssh, so a card fed from it showed the previous
    # reply for most of a minute and could never follow the sentence being
    # spoken. The broker has both, written as it speaks.
    _player(monkeypatch, volume=150.0,
            **{"user-data/agent-media/text": "the sentence being spoken",
               "media-title": "add C function"})
    monkeypatch.setattr(sc, "_clips",
                        lambda *a, **kw: pytest.fail("history was asked first"))
    got = sc._speech()
    assert got["title"] == "the sentence being spoken"
    assert got["conversation"] == "add C function"


def test_a_player_with_nothing_to_say_falls_back_to_the_history(monkeypatch):
    # A broker that restarted since the last reply, or a lane that never wrote
    # these. The history survives the player, and on a render host it is asked
    # of the origin — slower, older, still right.
    _player(monkeypatch, volume=150.0)
    monkeypatch.setattr(sc, "_clips", lambda *a, **kw: [
        {"text": "what was said before", "window": "add C function", "ref": "1"}])
    got = sc._speech()
    assert got["title"] == "what was said before"
    assert got["conversation"] == "add C function"


def test_a_player_that_cannot_be_asked_still_draws_the_card(monkeypatch):
    monkeypatch.setattr(
        "agent_media_core.cli._speech_display_state",
        lambda **kw: (False, 12.0, 90.0, False, False, 1.0, True))

    def boom(sock, names, **kw):
        raise OSError("no socket")

    monkeypatch.setattr("agent_media_core.sinks._mpv_ipc.get_properties", boom)
    monkeypatch.setattr(sc, "_clips", lambda *a, **kw: [])
    assert sc._speech()["volume"] is None


def test_an_unreachable_hub_refuses_rather_than_replays_the_wrong_turn(monkeypatch):
    monkeypatch.setattr(sc, "_origin_host", lambda: "red5")
    monkeypatch.setattr(sc, "_ask_origin", lambda argv, **kw: None)
    with pytest.raises(sc.ControlError):
        sc.control("speech", "replay", "1")


def test_the_origin_answers_itself(monkeypatch):
    # No hop, no cache, no ssh: the hub reads its own store, and `--json` on it
    # must not forward or the two would ask each other in a circle.
    monkeypatch.setattr("agent_media_core.config.host_roles",
                        lambda path=None: {"render", "origin"})
    assert sc._origin_host() is None
    seen = []
    sc.control("speech", "replay", "1", runner=lambda argv: seen.append(argv) or 0)
    assert seen == [["replay", "1"]]


def test_a_standalone_host_has_nobody_to_ask(monkeypatch):
    # origin+render+observe on one machine is a configuration, not a mode.
    monkeypatch.setattr("agent_media_core.config.host_roles",
                        lambda path=None: {"render"})
    monkeypatch.setattr("agent_media_core.config.peer", lambda alias, path=None: None)
    assert sc._origin_host() is None


def test_a_history_read_that_explodes_is_still_an_empty_list(monkeypatch):
    def boom(n=20, **kw):
        raise OSError("state store gone")

    monkeypatch.setattr("agent_media_core.cli._speech_history", boom)
    assert sc.chapters("speech") == []


def test_a_chapter_read_that_explodes_is_still_an_empty_list(monkeypatch):
    def boom():
        raise OSError("bridge down")

    monkeypatch.setattr("agent_media_core.cli._music_mpv_chapters", boom)
    assert sc.chapters() == []


def test_the_mute_badge_counts_mutes(monkeypatch, tmp_path):
    """It counted the buckets, not what was in them, and so always said 2."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from agent_media_core.state import StateStore

    store = StateStore()
    assert store.list_mutes() == {"panes": {}, "sessions": {}}

    snap = sc.channels()["speech"]
    assert snap["muted_elsewhere"] == 0, "nothing muted must read as nothing"

    store.set_mute("pane", "%12", True)
    store.set_mute("session", "projects-agent-media", True)
    store.set_mute("pane", "%13", False)      # an override that says "do speak"
    assert sc.channels()["speech"]["muted_elsewhere"] == 2
