"""The radio station (agent_media_core.radio), with the phone faked.

The Mix listing, the player's properties, the queueing and the prefetch are
each replaced at the module's seams; what is pinned is what the station does
with them: stays a song ahead, follows a song that comes up, steps aside for
one it did not queue, and keeps a 👎'd song (and a twice-👎'd channel) off.
"""

from __future__ import annotations

import pytest

from agent_media_core import radio


def _song(i: int, channel: str = "Kenny Rogers") -> dict:
    return {"id": f"song{i:02d}xxxxx", "title": f"Song {i}", "channel": channel, "dur": 200}


SEED = {"id": "jGYJrjwiPC4", "title": "The Gambler", "channel": "Kenny Rogers", "dur": 211}
MIX = [SEED] + [_song(i, "Kenny Rogers" if i < 3 else f"Artist {i}") for i in range(1, 12)]


class Player:
    """A phone player: a playlist of ids and the index playing."""

    def __init__(self, first: str):
        self.items = [first]
        self.pos = 0
        self.t = 30.0
        self.labels: list = []
        self.seeks: list = []

    def props(self, where):
        if not self.items:
            return {"playlist-pos": -1, "playlist-count": 0, "idle-active": True}
        return {"path": f"http://localhost:6616/{self.items[self.pos]}.mka",
                "playlist-pos": self.pos, "playlist-count": len(self.items),
                "idle-active": False, "time-pos": self.t, "duration": 200.0}


@pytest.fixture(autouse=True)
def _youtube_on(monkeypatch):
    """The YouTube path is off unless a server turns it on; these are about
    a server that has."""
    monkeypatch.setenv("MEDIA_RADIO_YOUTUBE", "1")


@pytest.fixture()
def station(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    player = Player(SEED["id"])
    sent: list = []
    fetched: list = []
    mixes: list = []
    def mix(vid):
        # The seed's Mix is MIX; any other's, ten songs of its own.
        mixes.append(vid)
        return list(MIX) if vid == SEED["id"] else \
            [{**_song(i, f"Artist {i}"), "id": f"{vid[:5]}{i:02d}more"} for i in range(10)]

    monkeypatch.setattr(radio, "mix", mix)
    monkeypatch.setattr(radio, "_props", lambda where: player.props(where))

    refuse: list = []

    def send(where, song, replace=False):
        sent.append((song["id"], replace))
        if song["id"] in refuse:
            return False
        if replace:
            player.items, player.pos = [song["id"]], 0
        else:
            player.items.append(song["id"])
        return True

    monkeypatch.setattr(radio, "_send", send)
    monkeypatch.setattr(radio, "_prefetch", lambda where, song: (fetched.append(song["id"]), True)[1])
    monkeypatch.setattr(radio, "_label", lambda where, song: player.labels.append(song["id"]))
    monkeypatch.setattr(radio, "_note", lambda song: None)
    monkeypatch.setattr(radio, "_liked_ids", lambda: set())
    monkeypatch.setattr(radio, "_clear", lambda where: None)
    monkeypatch.setattr(radio, "_next", lambda where: None)
    monkeypatch.setattr(radio, "_seek", lambda where, ms: player.seeks.append(ms))
    player.paused_at: list = []
    monkeypatch.setattr(radio, "_pause", lambda where: player.paused_at.append(where))
    player.refuse = refuse
    return player, sent, fetched, mixes


def test_a_station_is_the_mix_without_its_seed(station):
    snap = radio.start(f"yt:https://www.youtube.com/watch?v={SEED['id']}", "sasonica", playing=True)
    assert snap["on"] and snap["seed"] == {"id": SEED["id"], "title": "The Gambler"}
    assert [r["id"] for r in snap["next"]] == [s["id"] for s in MIX[1:11]]
    assert snap["more"] == 1


def test_it_stays_one_song_queued_and_one_downloaded(station):
    player, sent, fetched, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    radio.tick()
    assert sent == [(MIX[1]["id"], False)]
    radio.tick()
    assert fetched == [MIX[2]["id"]]
    radio.tick()                   # nothing to do while the seed plays on
    assert len(sent) == 1 and fetched == [MIX[2]["id"]]
    states = [r["state"] for r in radio.snapshot()["next"][:3]]
    assert states == ["ready", "ready", None]


def test_a_song_that_comes_up_is_labelled_and_the_next_queued(station):
    player, sent, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    radio.tick()
    player.pos = 1
    radio.tick()
    assert player.labels == [MIX[1]["id"]]
    assert radio.read()["current"] == MIX[1]["id"]
    assert sent[-1] == (MIX[2]["id"], False)


def test_a_song_it_has_no_record_of_is_played_on(station):
    """A canvas restarted mid-queueing loses the record of a song the player
    got; the station turned itself off on it (29 Sep 2026). It plays on."""
    player, sent, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    player.items, player.pos = ["aVtOLfGT3_8"], 0
    radio.tick()
    assert radio.is_on() and radio.read()["current"] == "aVtOLfGT3_8"
    assert sent[-1] == (MIX[1]["id"], False)


def test_a_play_of_something_else_ends_it(station, monkeypatch):
    from agent_media_core import cli

    radio.start(SEED["id"], "sasonica", playing=True)
    monkeypatch.setattr(cli, "_resolve_music_where", lambda w: "rooms")
    monkeypatch.setattr(cli.StateStore, "set_music_intent", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_note_music_where", lambda *a: None)

    played: list = []
    monkeypatch.setattr(cli, "SinkMusicRouter", lambda m: type("M", (), {
        "play": lambda self, uri, *a, **k: played.append(uri)})())
    cli.cmd_music(type("A", (), {"action": "play", "uri": "yt:abc", "add": False,
                                 "where": "rooms", "as_type": None, "title": ""})())
    assert played == ["yt:abc"]
    assert radio.is_on() is False


def test_dislike_skips_and_keeps_the_song_off(station):
    player, sent, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    radio.tick()
    radio.dislike()
    assert player.pos == 0 or player.items[player.pos] == MIX[1]["id"]
    assert SEED["id"] in radio.read()["banned"]


def test_two_thumbs_down_on_a_channel_keep_it_off(station):
    player, sent, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    radio.dislike()                # the seed: Kenny Rogers, strike one
    radio.tick()
    radio.dislike()                # Song 1: Kenny Rogers, strike two
    left = [s["channel"] for s in radio.read()["queue"]]
    assert "Kenny Rogers" not in left and left


def test_a_short_list_is_topped_up_from_another_mix(station):
    _, _, _, mixes = station
    radio.start(SEED["id"], "sasonica", playing=True)
    player, _, _, _ = station
    for _ in range(60):
        radio.tick()
        if player.pos + 1 < len(player.items):
            player.pos += 1        # each queued song is heard at once
    assert len(mixes) >= 2 and len(mixes) == len(set(mixes))


def test_off_leaves_the_music_playing(station):
    radio.start(SEED["id"], "sasonica", playing=True)
    radio.stop()
    assert radio.is_on() is False


def test_only_a_youtube_track_on_the_phone(station):
    with pytest.raises(ValueError):
        radio.start("/storage/music/song.mp3", "sasonica", playing=True)
    with pytest.raises(ValueError):
        radio.start(SEED["id"], "rooms", playing=False)


def test_a_station_runs_on_the_player_its_seed_is_on(station, monkeypatch):
    """Plush played in the Termux mpv (the app had not taken it), and the
    station went to the app's player behind it (David, 29 Sep 2026)."""
    player, _, _, _ = station
    real = player.props
    monkeypatch.setattr(radio, "_props", lambda where: real(where) if where == "phone"
                        else {"playlist-pos": -1, "playlist-count": 0, "idle-active": True})
    radio.start(SEED["id"], "sasonica", playing=True)
    assert radio.read()["where"] == "phone"
    monkeypatch.setattr(radio, "_props", lambda where: None)
    radio.start(SEED["id"], "sasonica", playing=True)
    assert radio.read()["where"] == "sasonica"       # neither says: the default


def test_a_song_cut_off_is_put_on_again_where_it_was(station):
    """An app update restarts its player empty (David, 29 Sep 2026: "It got
    interrupted towards the end with an update... continue from where it
    was at please")."""
    player, sent, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    player.t = 150.0
    radio.tick()                   # seen at 2:30, the next queued
    player.items, player.pos = [], 0
    radio.tick()
    assert sent[-1] == (SEED["id"], True)
    assert player.seeks == [150000] and player.labels[-1] == SEED["id"]
    radio.tick()                   # and the station goes on from there
    assert sent[-1] == (MIX[1]["id"], False)


def test_a_song_played_out_is_not_put_on_again(station):
    player, sent, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    player.t = 196.0
    radio.tick()
    player.items, player.pos = [], 0
    radio.tick()
    assert (SEED["id"], True) not in sent and player.seeks == []


def test_a_refused_song_is_tried_again_then_dropped(station):
    player, sent, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    player.refuse.append(MIX[1]["id"])
    for _ in range(3):
        radio.tick()
    assert [x for x in sent if x[0] == MIX[1]["id"]] == [(MIX[1]["id"], False)] * 3
    radio.tick()
    assert sent[-1] == (MIX[2]["id"], False)
    assert MIX[1]["id"] not in [r["id"] for r in radio.snapshot()["next"]]


def test_idle_with_a_queue_is_between_two_songs(station):
    """The app's player can say idle between two songs with the next still
    queued; that is not a lost song, and the next one is the station's."""
    player, sent, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    radio.tick()                   # MIX[1] queued
    real = player.props
    player.props = lambda where: {"playlist-pos": 0, "playlist-count": 2, "idle-active": True}
    radio.tick()
    assert (SEED["id"], True) not in sent
    player.props = real


def test_its_own_song_from_the_list_does_not_turn_it_off(station):
    player, _, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    player.items, player.pos = [MIX[3]["id"]], 0     # still on the list
    radio.tick()
    assert radio.is_on() and radio.read()["current"] == MIX[3]["id"]
    assert MIX[3]["id"] not in [s["id"] for s in radio.read()["queue"]]


def test_a_song_tapped_on_the_list_plays_now(station):
    """A tap on Up next (David, 29 Sep 2026: "I'd like to be able to click on
    things on the up next list")."""
    player, sent, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    radio.tick()                   # MIX[1] queued in the player
    radio.play(MIX[4]["id"])
    assert sent[-1] == (MIX[4]["id"], True) and player.labels[-1] == MIX[4]["id"]
    st = radio.read()
    assert st["current"] == MIX[4]["id"]
    # The songs before it stay to come, the one the replace cleared first.
    assert [r["id"] for r in radio.snapshot()["next"][:4]] == [s["id"] for s in (MIX[1], MIX[2], MIX[3], MIX[5])]
    radio.tick()                   # and the station goes on from there
    assert sent[-1] == (MIX[1]["id"], False) and radio.is_on()


def test_only_a_song_on_the_list_can_be_tapped(station):
    radio.start(SEED["id"], "sasonica", playing=True)
    with pytest.raises(ValueError):
        radio.play(SEED["id"])     # heard already
    with pytest.raises(ValueError):
        radio.play("notonthelst")
    radio.stop()
    assert radio.play(MIX[2]["id"]) is None


def test_a_tapped_song_the_player_refuses_stays_on_the_list(station):
    player, _, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    player.refuse.append(MIX[3]["id"])
    with pytest.raises(RuntimeError):
        radio.play(MIX[3]["id"])
    assert [s["id"] for s in radio.read()["queue"]][:3] == [MIX[1]["id"], MIX[2]["id"], MIX[3]["id"]]
    assert radio.read()["current"] == SEED["id"]


@pytest.fixture()
def dj(station, monkeypatch):
    """The DJ's picks faked: six songs a call, and what it was shown."""
    from agent_media_core import radio_dj

    asked: list = []

    def picks(st, n=6):
        k = len(asked)
        asked.append(st)
        return [{"id": f"dj{k}{i:02d}xxxxxx"[:11], "title": f"Pick {k}.{i}",
                 "channel": f"Artist {k}{i}", "dur": 200} for i in range(n)], f"mood {k}"

    monkeypatch.setattr(radio_dj, "picks", picks)
    return station, asked


def test_a_dj_station_puts_its_first_pick_on_in_place_of_what_plays(dj):
    (player, sent, _, _), asked = dj
    snap = radio.start_dj("sasonica")
    assert snap["on"] and snap["kind"] == "dj" and snap["seed"]["title"] == "Claude DJ"
    assert sent == []              # what was playing plays on until then
    radio.tick()
    assert len(asked) == 1 and sent == [("dj000xxxxxx", True)]
    assert radio.read()["current"] == "dj000xxxxxx" and player.labels[-1] == "dj000xxxxxx"
    snap = radio.snapshot()
    assert snap["note"] == "mood 0" and [r["title"] for r in snap["next"]][:2] == ["Pick 0.1", "Pick 0.2"]
    assert snap["current"]["id"] == "dj000xxxxxx"   # named even when the player can't be read
    radio.tick()                   # then as any station: one queued behind
    assert sent[-1] == ("dj001xxxxxx", False)


def test_the_dj_is_asked_again_when_the_list_runs_low(dj, monkeypatch):
    (player, _, _, _), asked = dj
    radio.start_dj("sasonica")
    for _ in range(40):
        radio.tick()
        with radio._station() as st:
            st["refilled"] = 0     # no minute to wait in a test
        if player.pos + 1 < len(player.items):
            player.pos += 1
    assert len(asked) >= 2
    assert asked[-1]["played"], "it is shown what has played"


def test_a_dj_that_finds_nothing_gives_up(station, monkeypatch):
    from agent_media_core import radio_dj

    monkeypatch.setattr(radio_dj, "picks", lambda st, n=6: ([], ""))
    radio.start_dj("sasonica")
    for _ in range(3):
        radio.tick()
        with radio._station() as st:
            st["refilled"] = 0
    assert radio.is_on() is False


def test_the_dj_reads_its_lines(station, monkeypatch):
    from agent_media_core import radio_dj

    monkeypatch.setattr(radio_dj, "moment", lambda: {"time_of_day": "afternoon"})
    monkeypatch.setattr(radio_dj, "_likes", lambda: [])
    import agent_media_core.intake._summary as s
    real = s._chat
    s._chat = lambda *a, **k: "1. Eagles - Take It Easy\n- The Band - The Weight\nnonsense\n\nNOTE: an easy afternoon"
    try:
        lines, note = radio_dj.ask(radio._blank())
    finally:
        s._chat = real
    assert lines == ["Eagles - Take It Easy", "The Band - The Weight"] and note == "an easy afternoon"


def test_a_load_whose_answer_was_lost_is_not_sent_again(monkeypatch, tmp_path):
    """The player got the song; the answer timed out. Taken, once."""
    from agent_media_core.sinks import _mpv_ipc as ipc

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    loads: list = []

    class Sink:
        def _endpoint(self):
            return "x"

        def play(self, uri, target, replace=False):
            loads.append(uri)
            raise ipc.MpvIpcError("timed out")

    from agent_media_core import radio_io
    monkeypatch.setattr(radio_io.PhonePlayer, "_sink", lambda self: Sink())
    monkeypatch.setattr(ipc, "get_property", lambda ep, name, **k:
                        [{"filename": "http://localhost:6616/_XC2mqcMMGQ.mka"}])
    assert radio._send("sasonica", {"id": "_XC2mqcMMGQ"}) is True
    assert len(loads) == 1
    monkeypatch.setattr(ipc, "get_property", lambda ep, name, **k: [])
    assert radio._send("sasonica", {"id": "_XC2mqcMMGQ"}) is False


def test_a_player_is_one_class_behind_the_seam(monkeypatch, tmp_path):
    """radio_io's seam (licensed-music proposal, step 1): a player registered
    there runs a station, and the station's code is not patched at all."""
    from agent_media_core import radio_io

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(radio_io, "youtube_mix", lambda vid: list(MIX))
    monkeypatch.setattr(radio, "_note", lambda song: None)

    class Handoff:
        items: list = [SEED["id"]]

        def __init__(self, where):
            pass

        def props(self):
            return {"path": self.items[-1], "playlist-pos": len(self.items) - 1,
                    "playlist-count": len(self.items), "idle-active": False, "time-pos": 5.0}

        def send(self, song, replace=False):
            Handoff.items.append(song["id"])
            return True

        def prefetch(self, song):
            return True

        def label(self, song):
            pass

        def clear(self):
            pass

        def next(self):
            pass

        def seek(self, ms):
            pass

    monkeypatch.setitem(radio_io.PLAYERS, "handoff", Handoff)
    monkeypatch.setattr(radio, "_playing_on", lambda vid: None)
    radio.start(SEED["id"], "handoff", playing=True)
    radio.tick()                   # one queued behind the seed
    radio.tick()                   # it comes up; the next is queued
    assert Handoff.items == [SEED["id"], MIX[1]["id"], MIX[2]["id"]]
    assert radio.read()["current"] == MIX[1]["id"]
    with pytest.raises(ValueError):
        radio_io.player("nowhere")


def test_youtube_radio_is_off_unless_the_server_turns_it_on(station, monkeypatch):
    """Licensing proposal step 2: no station on a server that has not said so,
    and a station on when it is switched off ends, the song playing on."""
    player, sent, _, _ = station
    monkeypatch.delenv("MEDIA_RADIO_YOUTUBE")
    assert radio.available() is False
    assert radio.snapshot()["available"] is False
    with pytest.raises(ValueError):
        radio.start(SEED["id"], "sasonica", playing=True)
    with pytest.raises(ValueError):
        radio.start_dj("sasonica")
    monkeypatch.setenv("MEDIA_RADIO_YOUTUBE", "1")
    radio.start(SEED["id"], "sasonica", playing=True)
    assert radio.snapshot()["available"] is True
    monkeypatch.setenv("MEDIA_RADIO_YOUTUBE", "0")
    radio.tick()
    assert radio.is_on() is False and sent == []


# ---- the hand-off player (licensed-music proposal, step 3) --------------------

def test_a_songs_query_is_the_djs_line_or_its_cleaned_title():
    from agent_media_core import radio_io

    assert radio_io.query({"q": "Eagles - Take It Easy", "title": "x"}) == \
        ("Eagles - Take It Easy", "Eagles", "Take It Easy")
    assert radio_io.query({"title": "Fleetwood Mac - Dreams (Official Music Video) [4K]",
                           "channel": "Fleetwood Mac"})[0] == "Fleetwood Mac - Dreams"
    assert radio_io.query({"title": "Lean on Me", "channel": "Bill Withers - Topic"}) == \
        ("Bill Withers - Lean on Me", "Bill Withers", "Lean on Me")
    assert radio_io.query({"title": "Lucille", "channel": ""}) == ("Lucille", "", "Lucille")


def test_a_search_result_keeps_the_line_it_answered():
    from agent_media_core import radio_io

    songs = radio_io.parse("5igDtWadYms\tEagles - Take it Easy (Official Audio)\tEagles\t213\tEagles - Take It Easy\n")
    assert songs[0]["q"] == "Eagles - Take It Easy" and songs[0]["dur"] == 213


def test_the_hand_off_player_asks_by_name(monkeypatch):
    from agent_media_core import radio_io
    from agent_media_core.sinks import _mpv_ipc as ipc

    monkeypatch.setenv("MEDIA_RADIO_HANDOFF_ENDPOINT", "tcp://phone:6617")
    sent: list = []
    monkeypatch.setattr(ipc, "command", lambda ep, *a, **k: sent.append((ep, *a)))
    p = radio_io.player("handoff")
    assert p.send({"id": "5igDtWadYms", "q": "Eagles - Take It Easy"}) is True
    ep, verb, uri, mode = sent[0]
    assert (ep, verb, mode) == ("tcp://phone:6617", "loadfile", "append-play")
    assert uri == "handoff/5igDtWadYms?q=Eagles+-+Take+It+Easy&artist=Eagles&title=Take+It+Easy"
    assert radio_io.vid_of(uri) == "5igDtWadYms"
    assert p.prefetch({"id": "x"}) is True and p.personal is False


def test_a_dj_on_the_hand_off_player_needs_no_youtube(station, monkeypatch):
    """No MEDIA_RADIO_YOUTUBE: the DJ's lines go to the music app by name."""
    from agent_media_core import radio_dj, radio_io

    player, sent, _, _ = station
    monkeypatch.delenv("MEDIA_RADIO_YOUTUBE")
    monkeypatch.setenv("MEDIA_RADIO_HANDOFF_ENDPOINT", "tcp://phone:6617")
    assert radio.available() is True
    assert radio_io.default_player("sasonica") == "handoff"
    monkeypatch.setattr(radio_dj, "ask", lambda st, n=6: (["Eagles - Take It Easy", "The Band - The Weight"], "easy"))
    monkeypatch.setattr(radio_dj, "search", lambda lines: (_ for _ in ()).throw(AssertionError("YouTube asked")))
    with pytest.raises(ValueError):
        radio.start_dj("sasonica")             # the phone's players need YouTube
    radio.start_dj("handoff")
    radio.tick()
    first = radio_io.synthetic_id("Eagles - Take It Easy")
    assert sent == [(first, True)]
    q = radio.read()["queue"][0]
    assert q["q"] == "The Band - The Weight" and q["channel"] == "The Band"
    radio.tick()
    assert radio.is_on()                        # no switch turns it off


def test_a_paused_song_cut_off_comes_back_paused(station):
    """An install restarted the app while a song sat paused; the station put
    it on again playing (30 Sep 2026)."""
    player, sent, _, _ = station
    radio.start(SEED["id"], "sasonica", playing=True)
    player.t = 100.0
    real = player.props
    player.props = lambda where: {**real(where), "pause": True}
    radio.tick()
    player.props = real
    player.items, player.pos = [], 0
    radio.tick()
    assert sent[-1] == (SEED["id"], True) and player.seeks == [100000]
    assert player.paused_at == ["sasonica"]
